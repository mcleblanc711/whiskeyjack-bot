"""Command-line entry point.

Subcommands are registered incrementally as their backlog issues land;
the scaffold ships only the program frame (M0-001).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from whiskeyjack_bot import __version__

if TYPE_CHECKING:
    import sqlite3

    from whiskeyjack_bot.config import AppConfig
    from whiskeyjack_bot.lifecycle import ApprovalDecision
    from whiskeyjack_bot.show import AnyStoredScore, HistoryEntry, RecordShow
    from whiskeyjack_bot.submission_payload import AuthorizedPayload

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

    init_ledger = subparsers.add_parser(
        "init-ledger",
        help="create or upgrade the ledger database at storage.sqlite_path; a no-op if current",
    )
    init_ledger.add_argument(
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

    submit = subparsers.add_parser(
        "submit",
        help="post one approved forecast to Metaculus and verify it by refetch",
    )
    submit.add_argument("--config", default="config.yaml", type=Path)
    submit.add_argument("--record-id", required=True, help="the approved forecast record to post")
    submit.add_argument(
        "--payload-file",
        type=Path,
        help=(
            "JSON file holding the Metaculus request payload: question_type plus one of "
            "probability_yes / continuous_cdf / probability_yes_per_category. Optional "
            "since M2-707: omit it and the payload the record derives is used, which is "
            "the only payload the approval authorizes anyway. Supply one to check a "
            "payload against that approval without posting anything it did not cover"
        ),
    )

    verify = subparsers.add_parser(
        "verify-submission",
        help="refetch an uncertain submission attempt and record what the platform shows",
    )
    verify.add_argument("--config", default="config.yaml", type=Path)
    verify.add_argument("--record-id", required=True, help="the forecast record to check")
    verify.add_argument(
        "--attempt-id",
        required=True,
        help="the uncertain attempt to resolve; submit prints it when it leaves one open",
    )

    ingest = subparsers.add_parser(
        "ingest-resolutions",
        help=(
            "fetch the resolution state of every submitted forecast and append it to the "
            "ledger; reads Metaculus only, posts nothing, makes no paid call"
        ),
    )
    ingest.add_argument("--config", default="config.yaml", type=Path)
    ingest.add_argument(
        "--question-id", type=int, help="restrict to the forecast records of one question"
    )

    score = subparsers.add_parser(
        "score",
        help=(
            "compute local Brier and log scores for resolved binary and multiple-choice "
            "forecasts and append them to the ledger; no network, no paid call"
        ),
    )
    score.add_argument("--config", default="config.yaml", type=Path)
    score.add_argument("--record-id", help="score only this forecast record")

    # Two commands, and the split is the safety property: `run` spends money and
    # `run-replay` cannot. T-903 shipped the replay path under the name `run` because it was
    # the only one that existed; `CODEX_HANDOFF.md` line 274 always meant the live one. With
    # both present, the difference between billing a provider and not billing one is a
    # different word on the command line rather than an omitted flag -- so a command line
    # cannot become a paid run by leaving something out.
    run = subparsers.add_parser(
        "run",
        help=(
            "forecast one or more saved questions through LIVE retrieval and a LIVE model "
            "call; this command spends money and never submits"
        ),
    )
    run.add_argument("--config", default="config.yaml", type=Path)
    run.add_argument(
        "--snapshot", required=True, type=Path, help="the saved question snapshot to load from"
    )
    selection = run.add_mutually_exclusive_group()
    selection.add_argument(
        "--question-id", type=int, help="forecast exactly this question from the snapshot"
    )
    selection.add_argument(
        "--limit",
        type=int,
        help=(
            "how many of the snapshot's supported questions to forecast, in order; may "
            "only lower run_limits.max_questions, never raise it"
        ),
    )
    run.add_argument(
        "--refresh-research",
        action="store_true",
        help=(
            "retrieve again even when the ledger already holds completed research for the "
            "question; without it a rerun repeats no paid retrieval call"
        ),
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="assert submission.dry_run is set; refuses if it is not",
    )
    run.add_argument(
        "--no-submit",
        action="store_true",
        help="assert submission.no_submit is set; refuses if it is not",
    )

    run_replay = subparsers.add_parser(
        "run-replay",
        help=(
            "forecast one saved question from replayed research and a saved model reply; "
            "makes no provider call and never submits"
        ),
    )
    run_replay.add_argument("--config", default="config.yaml", type=Path)
    run_replay.add_argument(
        "--question-id", required=True, type=int, help="the question in the snapshot to forecast"
    )
    run_replay.add_argument(
        "--snapshot", required=True, type=Path, help="the saved question snapshot to load from"
    )
    run_replay.add_argument(
        "--attempt-id",
        required=True,
        help=(
            "the saved attempt whose model reply to replay; the record this writes is "
            "stamped with a freshly minted attempt id of its own"
        ),
    )
    run_replay.add_argument(
        "--dry-run",
        action="store_true",
        help="assert submission.dry_run is set; refuses if it is not",
    )
    run_replay.add_argument(
        "--no-submit",
        action="store_true",
        help="assert submission.no_submit is set; refuses if it is not",
    )

    release = subparsers.add_parser(
        "release-key",
        help="give up a standing idempotency-key reservation left by an interrupted submit",
    )
    release.add_argument("--config", default="config.yaml", type=Path)
    release.add_argument(
        "--record-id", required=True, help="the forecast record whose reservation to release"
    )
    release.add_argument(
        "--released-by",
        required=True,
        help=(
            "who is asserting that nothing was posted; recorded verbatim. Required and "
            "with no default, for `approve`'s reason: this is a claim about what a person "
            "checked, and the program cannot make it"
        ),
    )
    release.add_argument("--note", help="optional free-text note, stored with the release")
    release.add_argument(
        "--reservation-id",
        help=(
            "which reservation to release; needed only when the record holds more than "
            "one, which the command lists rather than guessing between"
        ),
    )

    reconcile = subparsers.add_parser(
        "reconcile-submission",
        help=(
            "record that a live post reached Metaculus and was never written to the ledger; "
            "one refetch, no post"
        ),
    )
    reconcile.add_argument("--config", default="config.yaml", type=Path)
    reconcile.add_argument(
        "--record-id", required=True, help="the approved forecast record whose post is unrecorded"
    )
    reconcile.add_argument(
        "--observed-by",
        required=True,
        help=(
            "who checked Metaculus and saw this forecast there; recorded verbatim. Required and "
            "with no default, for `approve`'s reason: this is a claim about what a person "
            "checked, and the program cannot make it"
        ),
    )
    reconcile.add_argument(
        "--note",
        required=True,
        help="what you saw on the platform; required, because an assertion with no content is none",
    )

    unrecorded = subparsers.add_parser(
        "unrecorded-posts",
        help=(
            "list records holding a submission intent and a standing, unspent key reservation; "
            "reads the ledger only"
        ),
    )
    unrecorded.add_argument("--config", default="config.yaml", type=Path)

    replay = subparsers.add_parser(
        "replay",
        help="re-derive a stored forecast from its saved model output; makes no API call",
    )
    replay.add_argument("--config", default="config.yaml", type=Path)
    replay.add_argument("--record-id", required=True, help="the forecast record to replay")

    show = subparsers.add_parser(
        "show",
        help="render one forecast record's derived status, approval, lifecycle history, "
        "unresolved uncertainties and standing key reservations; reads the ledger only",
    )
    show.add_argument("--config", default="config.yaml", type=Path)
    show.add_argument("--record-id", required=True, help="the forecast record to show")

    tournament = subparsers.add_parser("tournament", help="activated tournament operation")
    commands = tournament.add_subparsers(dest="tournament_command", required=True)
    for name in ("run-once", "enable", "disable", "status", "reconcile-restored", "correct-costs"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config.yaml"))
        if name == "correct-costs":
            # M1-348: dry run by default, and a dry run opens the ledger read-only.
            command.add_argument(
                "--apply",
                action="store_true",
                help="append the corrections; without it, report them and write nothing",
            )
        if name == "run-once":
            command.add_argument(
                "--question-id",
                type=int,
                help="restrict discovery to one subquestion for rehearsal",
            )
        if name == "enable":
            command.add_argument("--project-id", type=int, required=True)
            command.add_argument("--starts", required=True, help="UTC ISO timestamp")
            command.add_argument("--ends", required=True, help="UTC ISO timestamp")
            command.add_argument("--budget-usd", type=float, default=20.0)

    export = subparsers.add_parser(
        "export",
        help="write the ledger out as derived JSONL or Parquet files; never writes to it",
    )
    export.add_argument("--config", default="config.yaml", type=Path)
    export.add_argument(
        "--format",
        dest="export_format",
        required=True,
        choices=("jsonl", "parquet"),
        help="jsonl for audit and interchange, parquet for analysis",
    )
    export.add_argument(
        "--output",
        type=Path,
        help=(
            "directory to create the export in; defaults to a new UTC-timestamped "
            "directory under storage.export_root. An existing export is never overwritten"
        ),
    )

    report = subparsers.add_parser(
        "report",
        help=(
            "derive the attribution report dataset (counts, calibration bins, score "
            "summaries) from the ledger; never writes to it"
        ),
    )
    report.add_argument("--config", default="config.yaml", type=Path)
    report.add_argument(
        "--output",
        type=Path,
        help=(
            "directory to create the report in; defaults to a new UTC-timestamped "
            "directory under storage.export_root. An existing report is never overwritten"
        ),
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


def _run_init_ledger(args: argparse.Namespace) -> int:
    """Create-or-upgrade the ledger schema (M2-712); the first-time path ``run``,
    ``approve``, ``submit`` etc. all need but none of them provide.

    Calls ``ledger.initialize_ledger()`` directly and reports what it returns --
    idempotency is that function's guarantee, not reimplemented here. Deliberately does
    not go anywhere near ``_open_existing_ledger``/``open_verified_ledger``: those exist
    precisely to refuse a database that isn't there yet, which is the case this command
    is for.
    """
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.ledger import LedgerError, initialize_ledger
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

    try:
        version = initialize_ledger(config.storage.sqlite_path)
    except LedgerError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED

    print(f"ledger:  {config.storage.sqlite_path}")
    print(f"version: {version}")
    return EXIT_OK


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

    Prints identity, derived status and the content hash before writing, and -- since
    M2-707 -- **the payload the decision authorized and its digest** immediately after,
    which is the acceptance criterion's "an operator can see what a decision authorized".
    It is printed in full rather than summarized: the digest alone says two payloads
    differ, and the JSON says how.

    **The payload line comes after the write, and that is the M2-707 round-1 fix showing
    through.** :func:`approval.approve` now derives the payload itself, inside the
    transaction that writes the decision, because a digest the *caller* chose cannot
    establish a claim about what the record derives. So this command has nothing to print
    until the decision exists -- and what it prints is the derivation that was stored,
    rather than a second run of the same function whose agreement with the stored one it
    would then be asserting. A build failure refuses the whole command and writes nothing
    (the transaction rolls back), so a numeric record whose percentiles no longer convert
    still cannot be approved.

    ``reject`` skips every line of that. A rejection authorizes nothing, and a record whose
    payload cannot be built must still be rejectable -- which is why ``011`` requires the
    digest for one decision and forbids it for the other, and why ``reject`` takes no
    ``calibration``.

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

        try:
            if decision == "approved":
                # `approve` derives the payload itself, inside the transaction that writes
                # the decision (M2-707 round 1), so what is printed below is the derivation
                # that was *stored* rather than a second run of the same function. The
                # command no longer derives one to show first: it could only have shown a
                # value it then asked another function to reproduce.
                approved = approve(
                    connection,
                    record_id=args.record_id,
                    actor=args.actor,
                    occurred_at=datetime.now(tz=timezone.utc),
                    calibration=config.numeric_calibration,
                    note=args.note,
                    expected_sha256=args.forecast_sha256,
                )
                recorded = approved.decision
                print(f"payload:   sha256 {approved.authorized.sha256}")
                print(f"           {approved.authorized.canonical}")
            else:
                recorded = reject(
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


def _derive_payload(
    connection: sqlite3.Connection, record_id: str, config: AppConfig
) -> AuthorizedPayload | None:
    """Return the payload a record derives -- mapping, bytes and digest -- or ``None``.

    Used by ``submit`` alone, to fill in an omitted ``--payload-file``. ``approve`` used to
    share it and no longer does: :func:`approval.approve` derives its own payload inside
    the transaction that writes the decision (M2-707 round 1), because a digest a caller
    chose cannot establish what a record derives. The two paths therefore call the same
    ``authorized_payload`` on the same record and cannot disagree -- the function is
    deterministic in ``(record, calibration)`` -- but ``submit``'s result is a *proposal*
    checked against the stored digest at the key seam, while ``approve``'s is the stored
    digest.

    All three fields travel together because the printed JSON, the compared digest and the
    posted mapping have to be the same payload;
    :class:`~submission_payload.AuthorizedPayload` renders once and digests that rendering,
    so they are the same bytes rather than three dumps of one object.

    Refusals print and return ``None`` rather than raising, matching every other refusal in
    this module. Nothing about the payload's *content* is echoed: ``PayloadBuildError``'s
    messages name fields and rules, never values.
    """
    from whiskeyjack_bot.forecast.record import ForecastRecordError
    from whiskeyjack_bot.forecast.store import read_forecast_record
    from whiskeyjack_bot.submission import SubmissionError
    from whiskeyjack_bot.submission_payload import authorized_payload

    try:
        record = read_forecast_record(connection, record_id)
    except ForecastRecordError as exc:
        print(f"refused: {exc}")
        return None
    try:
        return authorized_payload(record, calibration=config.numeric_calibration)
    except SubmissionError as exc:
        print(f"refused: {exc}")
        return None


def _run_submit(args: argparse.Namespace) -> int:
    """Post one approved forecast, and print what was recorded (M2-704).

    **This is the only command in the tree that can cause a live Metaculus post**, and it
    is arranged so an operator sees what is about to happen before it does: the record's
    identity, its derived status, the hash the approval binds to, the payload digest and
    the derived idempotency key are all printed first. That is ``approve``'s shape and it
    is here for the same reason -- a submission whose payload the operator never saw
    described is an attribution claim with nothing behind it.

    **``--payload-file`` is optional since M2-707**, and the asymmetry is the point. Omit
    it and the payload the record derives is used -- which, now that an approval binds to a
    payload digest, is the only payload that can reach a post anyway; requiring an operator
    to hand-write a 201-point CDF that hashes identically would have made the command
    undrivable for numeric questions. Supply one and it is checked against that approval and
    refused if it is not what was authorized, which is the acceptance criterion's *"a
    submission payload that does not derive from the approved forecast is refused before any
    post"* made reproducible from the command line rather than only from a test.

    Either way the digest is printed before anything else happens, and either way the gate
    is :func:`submission.submission_key_for_approved_record`. This command never decides
    that a payload is authorized; it only decides which one to offer.

    Every refusal is ``EXIT_REFUSED`` and prints why. A refusal from
    :func:`submission_live.post_approved_forecast` means nothing was posted -- every gate
    it applies runs in front of the post -- with one exception the message names: a post
    the ledger then refused to record, which is the one case where an error follows a live
    call.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.approval import ApprovalError, read_forecast_summary
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.metaculus.client import MissingCredentialError, build_poster
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.submission import SubmissionError
    from whiskeyjack_bot.submission_gateway import payload_sha256
    from whiskeyjack_bot.submission_live import (
        LiveSubmissionError,
        post_approved_forecast,
        require_live_submission_enabled,
    )

    try:
        config = _load_verified_config(args.config)
    except ConfigError as exc:
        print(exc)
        return EXIT_CONFIG_INVALID
    except AllowlistError as exc:
        print(exc)
        return EXIT_ENV_MISSING if exc.is_filesystem_error else EXIT_CONFIG_INVALID
    configure_logging(config)

    # Read before the ledger is opened, because a caller mistake should be refused without
    # a read -- M1-303 round 4's rule, already applied to every other argument here. The
    # *derived* payload cannot follow it: deriving one needs the record.
    supplied: dict[str, object] | None = None
    if args.payload_file is not None:
        supplied = _read_payload_file(args.payload_file)
        if supplied is None:
            return EXIT_REFUSED

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
        # M1-508/D45: what a numeric CDF was built from, recorded beside the post. Known
        # only for a payload derived here; a supplied file carries none, and the artifact
        # then has no record rather than a claim that nothing was adjusted.
        conversion: Mapping[str, object] | None = None
        if supplied is None:
            derived = _derive_payload(connection, args.record_id, config)
            if derived is None:
                return EXIT_REFUSED
            payload, digest, source = derived.payload, derived.sha256, "derived"
            conversion = derived.conversion
        else:
            payload = supplied
            source = "from file"
            try:
                digest = payload_sha256(payload)
            except SubmissionError as exc:
                print(f"refused: {exc}")
                return EXIT_REFUSED
        print(f"payload:   sha256 {digest} ({source})")

        # Before the poster, because constructing one reads METACULUS_TOKEN: an operator
        # running this against the committed configuration should be told that submission
        # is off, not that a credential is missing. `post_approved_forecast` checks it
        # again as its first act.
        try:
            require_live_submission_enabled(config)
        except LiveSubmissionError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        try:
            poster = build_poster(config)
        except MissingCredentialError as exc:
            print(f"refused: {exc}")
            return EXIT_ENV_MISSING
        try:
            recorded = post_approved_forecast(
                connection,
                record_id=args.record_id,
                payload=payload,
                poster=poster,
                config=config,
                occurred_at=datetime.now(tz=timezone.utc),
                conversion=conversion,
            )
        except LiveSubmissionError as exc:
            print(f"refused: {exc}")
            # A refusal can leave the key claimed -- either because this command lost the
            # race for it, or because an earlier one was interrupted holding it. Say how
            # to get out, with the identifiers filled in: the library refusal deliberately
            # names no key (it is derived from a payload hash), so without this the
            # operator is told they are blocked and not what to do about it.
            _print_standing_reservations(connection, args.record_id)
            return EXIT_REFUSED
        receipt = recorded.receipt
        print(f"attempt:   {receipt.attempt_id}")
        print(f"key:       {receipt.idempotency_key}")
        print(
            f"result:    {recorded.event.event_type} "
            f"(success={receipt.success}, refetch={receipt.refetch_outcome})"
        )
        if receipt.error_type is not None:
            print(f"error:     {receipt.error_type}: {receipt.error_message}")
        if recorded.artifact_path is not None:
            print(f"artifact:  {config.storage.artifact_root / recorded.artifact_path}")
        else:
            print(f"artifact:  NOT WRITTEN -- {recorded.artifact_error}")
        if recorded.event.event_type == "submission_uncertain":
            print(
                "the outcome is unresolved; run "
                f"`whiskeyjack-bot verify-submission --record-id {summary.record_id} "
                f"--attempt-id {receipt.attempt_id}` before submitting anything else "
                "for this record"
            )
        return EXIT_OK if receipt.verified_by_refetch and recorded.artifact_path else EXIT_REFUSED
    finally:
        connection.close()


def _run_verify_submission(args: argparse.Namespace) -> int:
    """Refetch an uncertain attempt and record what the platform shows (M2-704).

    Reads only. It makes no post and reads no submission flags, so it is safe to run at any
    time -- which matters, because it is the command that reopens the gate
    :func:`submission_live.post_approved_forecast` closes after an uncertain outcome.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.metaculus.client import MissingCredentialError, build_poster
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.submission_live import LiveSubmissionError, verify_uncertain_attempt

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
            poster = build_poster(config)
        except MissingCredentialError as exc:
            print(f"refused: {exc}")
            return EXIT_ENV_MISSING
        try:
            event = verify_uncertain_attempt(
                connection,
                record_id=args.record_id,
                attempt_id=args.attempt_id,
                poster=poster,
                occurred_at=datetime.now(tz=timezone.utc),
            )
        except LiveSubmissionError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(f"record:    {args.record_id}")
        print(f"attempt:   {args.attempt_id}")
        print(f"result:    {event.event_type} (lifecycle seq {event.event_seq})")
        return EXIT_OK
    finally:
        connection.close()


def _run_ingest_resolutions(args: argparse.Namespace) -> int:
    """Fetch and record resolution state for submitted forecasts (M4-801).

    Reads Metaculus, writes the ledger. It builds a plain client rather than a poster, so no
    post method is reachable from here, and it reads no submission flag. Exits
    ``EXIT_REFUSED`` when any record was skipped, after recording every one it could, so a
    scheduled run that half-failed is not reported as a success.
    """
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.metaculus.client import MissingCredentialError, build_client
    from whiskeyjack_bot.notify import build_notifier, notifier_context
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.resolution_ingest import (
        ResolutionIngestError,
        ingest_resolutions,
        notify_withheld,
        sdk_post_fetcher,
        withheld_records,
    )

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
            client = build_client(config)
        except MissingCredentialError as exc:
            print(f"refused: {exc}")
            return EXIT_ENV_MISSING
        try:
            results = ingest_resolutions(
                connection, sdk_post_fetcher(client), question_id=args.question_id
            )
            withheld = withheld_records(connection, results)
        except ResolutionIngestError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        # M4-807. `emit` absorbs every failure, so the exit code below is decided by the
        # results alone. The notifier is built only when there is something to send, after
        # the ledger work is done.
        if withheld:
            with notifier_context(build_notifier(config)):
                notify_withheld(withheld)
        failed = 0
        for result in results:
            if result.status == "failed":
                failed += 1
                print(
                    f"question {result.question_id}  record {result.record_id}  failed: "
                    f"{result.detail}"
                )
                continue
            kind = "-" if result.kind is None else result.kind
            scorable = "-" if result.scorable is None else ("yes" if result.scorable else "no")
            moved = "  -> resolved" if result.moved_to_resolved else ""
            print(
                f"question {result.question_id}  record {result.record_id}  "
                f"{result.status}  kind {kind}  scorable {scorable}{moved}"
            )
        print(f"records: {len(results)}  failed: {failed}  withheld: {len(withheld)}")
        return EXIT_REFUSED if failed else EXIT_OK
    finally:
        connection.close()


def _run_score(args: argparse.Namespace) -> int:
    """Record local (M4-802) and platform (M4-803) scores for resolved forecasts.

    Reads and writes the ledger only. It builds no client and imports nothing that reaches
    the network, so there is no post method and no paid call anywhere on this path. Exits
    ``EXIT_REFUSED`` when any record failed, after scoring every one it could.
    """
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.score_records import ScoreRecordsError, score_records

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
            results = score_records(connection, record_id=args.record_id)
        except ScoreRecordsError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        failed = 0
        for result in results:
            prefix = (
                f"question {result.question_id}  record {result.record_id}  "
                f"{result.question_type}  "
            )
            local = f"{result.status}  rows {result.rows_appended}"
            platform = f"platform {result.platform_status}  rows {result.platform_rows_appended}"
            moved = "  -> scored" if result.moved_to_scored else ""
            if result.failed:
                failed += 1
                print(f"{prefix}{local}  {platform}  failed: {result.detail}")
                continue
            print(f"{prefix}{local}  {platform}{moved}")
        print(f"records: {len(results)}  failed: {failed}")
        return EXIT_REFUSED if failed else EXIT_OK
    finally:
        connection.close()


def _run_release_key(args: argparse.Namespace) -> int:
    """Give up a standing key reservation, so an interrupted forecast can be retried.

    **The state this exists for.** M2-708 makes `submit` claim its idempotency key before
    any network I/O, and the claim is a row because the failure it prevents is durable. A
    process killed between the claim and the attempt row therefore leaves a reservation
    with no attempt -- and a key is a pure function of the tournament, question, forecast
    version and payload hash, so the same work derives the same key forever. Without a way
    out, one interrupted command would block that forecast permanently, on an append-only
    table.

    **What the operator is asserting**, and why `--released-by` has no default: that they
    checked Metaculus and nothing landed. The program cannot make that claim -- when it
    *can* prove no post was made it releases the key itself, under `not_posted`, with no
    person named. This command is the other case, where the program knows nothing, so the
    release is an attribution claim about a human and is recorded as one. `approve`'s rule.

    **The one case where releasing is wrong** is a reservation left standing because the
    ledger refused to record a post that succeeded. There the post *did* land, and
    releasing would invite a duplicate. `submit` says so when it happens; the preamble
    below repeats it, because this command is reached long after that message scrolled by.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.submission import (
        SubmissionError,
        live_reservations_for_record,
        release_submission_key,
    )

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
            standing = live_reservations_for_record(connection, args.record_id)
        except SubmissionError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED

        if not standing:
            print(f"record:    {args.record_id}")
            print(
                "refused: no key reservation is standing for this record; there is "
                "nothing to release"
            )
            return EXIT_REFUSED

        if args.reservation_id is None and len(standing) > 1:
            # Never guess. Two live reservations means two payloads, and only the operator
            # knows which submission they went and checked.
            print(f"record:    {args.record_id}")
            print(
                f"refused: this record holds {len(standing)} standing reservations; "
                "re-run with --reservation-id naming the one you checked"
            )
            for held in standing:
                print(
                    f"  {held.reservation_id}  (seq {held.reservation_seq}, "
                    f"reserved {held.reserved_at_utc})"
                )
            return EXIT_REFUSED

        if args.reservation_id is None:
            reservation = standing[0]
        else:
            matched = [r for r in standing if r.reservation_id == args.reservation_id]
            if not matched:
                print(f"record:    {args.record_id}")
                print(
                    "refused: --reservation-id does not name a standing reservation for this record"
                )
                return EXIT_REFUSED
            reservation = matched[0]

        print(f"record:      {reservation.forecast_record_id}")
        print(
            f"reservation: {reservation.reservation_id}  (seq {reservation.reservation_seq}, "
            f"reserved {reservation.reserved_at_utc})"
        )
        print(
            "releasing records that you checked Metaculus and this forecast is NOT there. "
            "If submit told you a post was made and the ledger refused to record it, the "
            "post did land -- do not release; record it with reconcile-submission instead."
        )
        try:
            release_submission_key(
                connection,
                reservation,
                reason="operator_abandoned",
                released_at=datetime.now(tz=timezone.utc),
                released_by=args.released_by,
                note=args.note,
            )
        except SubmissionError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(
            f"released {reservation.reservation_id} (operator_abandoned, "
            f"by {args.released_by}); the key may be claimed again"
        )
        return EXIT_OK
    finally:
        connection.close()


def _run_reconcile_submission(args: argparse.Namespace) -> int:
    """Record a live post the ledger never wrote down (M2-713).

    **Posts nothing.** It reads the ledger and the artifact, prints the evidence it found, and
    only then builds a poster -- so every local refusal happens without a credential, which
    ``verify-submission`` does not manage -- and makes one identity read and one refetch.

    **What the operator is asserting**, and why ``--observed-by`` and ``--note`` have no
    default: that they looked at Metaculus and this forecast is there. The program checks that
    claim against its own refetch and its own pre-post evidence, and refuses when they
    disagree, but it cannot make the claim for them. The mirror of ``release-key``: that
    command records "I checked and it is not there".
    """
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.metaculus.client import MissingCredentialError, build_poster
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.submission_reconcile import (
        ReconciliationError,
        check_assertion,
        find_unrecorded_post,
        reconcile_unrecorded_post,
    )

    try:
        config = _load_verified_config(args.config)
    except ConfigError as exc:
        print(exc)
        return EXIT_CONFIG_INVALID
    except AllowlistError as exc:
        print(exc)
        return EXIT_ENV_MISSING if exc.is_filesystem_error else EXIT_CONFIG_INVALID
    configure_logging(config)
    # The assertion first: argparse's `required=True` accepts " ", and a blank assertion should
    # be refused before the ledger or the artifact is read (round 1).
    try:
        check_assertion(args.observed_by, args.note)
    except ReconciliationError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED

    connection = _open_existing_ledger(config.storage.sqlite_path)
    if connection is None:
        return EXIT_REFUSED
    try:
        print(f"record:      {args.record_id}")
        try:
            evidence = find_unrecorded_post(connection, config, args.record_id)
        except ReconciliationError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(f"question:    {evidence.question_id}  post: {evidence.post_id}")
        print(f"reservation: {evidence.reservation_id}  (reserved {evidence.reserved_at_utc})")
        print(f"key:         {evidence.idempotency_key}")
        print(f"attempt:     {evidence.attempt_id}")
        print(f"payload:     sha256 {evidence.request_payload_sha256}")
        print(f"intent:      {evidence.intent_event_id}  (account {evidence.account_id})")
        if evidence.artifact_path is None:
            print("artifact:    none -- the command stopped before the receipt was written")
        else:
            print(
                f"artifact:    {config.storage.artifact_root / evidence.artifact_path}  "
                f"(sha256 {evidence.artifact_sha256})"
            )
        print(
            "reconciling records that you checked Metaculus and this forecast IS there. If it "
            "is not, do not reconcile; release-key is the way out."
        )
        try:
            poster = build_poster(config)
        except MissingCredentialError as exc:
            print(f"refused: {exc}")
            return EXIT_ENV_MISSING
        try:
            event = reconcile_unrecorded_post(
                connection,
                config,
                record_id=args.record_id,
                observed_by=args.observed_by,
                note=args.note,
                poster=poster,
            )
        except ReconciliationError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(
            f"result:      {event.event_type} -> {event.to_status} "
            f"(reconciliation {event.submission_reconciliation_id}, lifecycle seq "
            f"{event.event_seq})"
        )
        return EXIT_OK
    finally:
        connection.close()


def _run_unrecorded_posts(args: argparse.Namespace) -> int:
    """List records that look like an unrecorded post (M2-713). Ledger reads only.

    Candidates, not verdicts: each is a record holding a submission intent and a standing,
    unspent reservation. The platform decides which command applies -- ``reconcile-submission``
    if the forecast is there, ``release-key`` if it is not.
    """
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.submission_reconcile import ReconciliationError, unrecorded_posts

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
            candidates = unrecorded_posts(connection)
        except ReconciliationError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        for record_id in candidates:
            print(f"record: {record_id}")
        print(f"unrecorded-post candidates: {len(candidates)}")
        if candidates:
            print(
                "check each on Metaculus: if the forecast is there, run reconcile-submission; "
                "if it is not, run release-key"
            )
        return EXIT_OK
    finally:
        connection.close()


def _print_standing_reservations(connection: object, record_id: str) -> None:
    """Tell the operator how to release a key reservation, if one is standing.

    Read-only and best-effort: it runs while a refusal is already being reported, so a
    ledger that cannot answer must not turn that refusal into a traceback. Silence is the
    right failure -- the refusal itself has already been printed.
    """
    import sqlite3

    from whiskeyjack_bot.submission import SubmissionError, live_reservations_for_record

    if not isinstance(connection, sqlite3.Connection):  # pragma: no cover - defensive
        return
    try:
        standing = live_reservations_for_record(connection, record_id)
    except SubmissionError:
        return
    if not standing:
        return
    print(
        f"a key reservation is standing for this record ({len(standing)}); if you have "
        "confirmed nothing was posted, run"
    )
    for held in standing:
        suffix = f" --reservation-id {held.reservation_id}" if len(standing) > 1 else ""
        print(f"  whiskeyjack-bot release-key --record-id {record_id} --released-by <you>{suffix}")
    # M2-713. The other answer to the same check: the forecast IS on Metaculus. Releasing then
    # would invite a duplicate, and until reconcile-submission there was nothing to run.
    print("if the forecast IS on Metaculus, do not release; record the post with")
    print(
        f"  whiskeyjack-bot reconcile-submission --record-id {record_id} "
        '--observed-by <you> --note "<what you saw>"'
    )


def _read_payload_file(path: Path) -> dict[str, object] | None:
    """Load a submission payload from disk, or print why not and return ``None``.

    Refuses anything the gateway would refuse later, but earlier and with the *path* in the
    message -- which is the M1-401 carve-out's whole justification: a "cannot read payload"
    with no path is not actionable. The file's *contents* are never echoed: a payload is
    content, and the gateway's own validators name fields rather than values for the same
    reason.
    """
    import json

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        print(f"refused: cannot read the payload file {path}")
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        print(f"refused: the payload file is not valid JSON: {path}")
        return None
    if not isinstance(payload, dict):
        print(f"refused: the payload file must hold a JSON object: {path}")
        return None
    return payload


def _print_question_outcome(outcome: object) -> None:
    """One question's block. Values only; every string here came back sanitized."""
    from whiskeyjack_bot.pipeline_live import QuestionOutcome

    if not isinstance(outcome, QuestionOutcome):  # pragma: no cover - defensive
        return
    print(f"question:  {outcome.question_id}")
    print(f"  attempt:  {outcome.attempt_id}")
    reused = " (reused, no provider call)" if outcome.research_reused else ""
    print(
        f"  research: {len(outcome.retrieval_run_ids)} run(s), "
        f"{outcome.document_count} source(s){reused}"
    )
    if outcome.status == "recorded":
        print(f"  record:   {outcome.record_id}  version: {outcome.forecast_version}")
        print(f"  packet:   {outcome.research_packet_sha256}")
        # The `(none: ...)` fallback that `run-replay` does **not** have, and the asymmetry
        # is M1-312's rule rather than an inconsistency: `run_replay` refuses a record whose
        # artifact was not written, so printing that state would describe something it
        # cannot produce. A paid attempt's row is written regardless -- the cost is a fact
        # even when the evidence copy is not -- so here the state is reachable and printing
        # it is the only way an operator learns the record cannot be replayed.
        print(f"  artifact: {outcome.raw_output_path or f'(none: {outcome.artifact_outcome})'}")
        print(f"  hash:     {outcome.forecast_sha256}")
        print("  status:   validated")
    else:
        detail = f" ({outcome.detail_code})" if outcome.detail_code else ""
        print(f"  status:   {outcome.status}{detail}")
        for problem in outcome.problems:
            print(f"    - {problem}")
        if outcome.note:
            print(f"    note: {outcome.note}")


def _run_run(args: argparse.Namespace) -> int:
    """Forecast one or more saved questions live, through paid retrieval and a paid call.

    **This command spends money**, which is why it is a different word from ``run-replay``
    rather than the same word with a flag. ``CODEX_HANDOFF.md`` line 274 always described
    the live command; T-903 shipped the replay path under this name because it was the only
    one that existed, and recorded the deviation and this item as its owner.

    It still **cannot submit**, and structurally rather than by a check: no submission and no
    approval module is on ``whiskeyjack_bot.pipeline_live``'s import path. ``--dry-run`` and
    ``--no-submit`` keep T-903's treatment exactly -- they *assert* the configuration and
    never override it, because a flag that silently forced the safe value would let a config
    with ``dry_run: false`` pass a command line that reads as safe.

    Every question gets its own block and the batch gets a summary, printed even when
    questions failed: a partial batch is the ordinary outcome of per-question isolation, and
    an operator needs the identity and hash of the records that *were* written -- their next
    command is ``approve --forecast-sha256 <hash>``. The exit code is ``EXIT_OK`` only when
    no question failed.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.pipeline_live import LiveRunError, run_live
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

    if args.dry_run and not config.submission.dry_run:
        print("refused: --dry-run was passed but submission.dry_run is not set")
        return EXIT_REFUSED
    if args.no_submit and not config.submission.no_submit:
        print("refused: --no-submit was passed but submission.no_submit is not set")
        return EXIT_REFUSED

    connection = _open_existing_ledger(config.storage.sqlite_path)
    if connection is None:
        return EXIT_REFUSED
    try:
        try:
            batch = run_live(
                connection,
                config,
                snapshot=args.snapshot,
                now=datetime.now(tz=timezone.utc),
                question_id=args.question_id,
                limit=args.limit,
                refresh_research=args.refresh_research,
            )
        except LiveRunError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        for outcome in batch.outcomes:
            _print_question_outcome(outcome)
        print(f"records:   {batch.records_written} of {len(batch.outcomes)} question(s)")
        # Two figures, never one. `cost_usd is None` means unknown, never free, so a single
        # total would have to invent a zero for every unpriced call -- and AskNews prices
        # none of its calls at all. Saying both is the only honest summary available.
        print(
            f"spend:     {batch.known_cost_usd:.4f} USD known, "
            f"{batch.unpriced_calls} unpriced call(s)"
        )
        print(f"stopped:   {batch.stop_reason}")
        print("submitted: no -- `run` never submits; approve and submit are separate commands")
        return EXIT_OK if batch.failures == 0 and batch.outcomes else EXIT_REFUSED
    finally:
        connection.close()


def _run_run_replay(args: argparse.Namespace) -> int:
    """Forecast one saved question from replayed research and a saved reply (T-903).

    The command T-903's criterion is about: *"one command, one saved question, research +
    model replay -> one complete validated ledger record, zero provider calls, zero
    submission calls, reproducible forecast hash."* The last clause is ``replay
    --record-id`` run afterwards on the record this prints.

    **Named ``run-replay`` since M1-315**, which gave ``run`` to the live paid composition
    that ``CODEX_HANDOFF.md`` line 274 always described. Nothing else about this command
    changed -- same flags, same refusals, same zeroes -- and the rename is what makes those
    zeroes legible from the command line rather than from the configuration file.

    **This command cannot submit**, and that is structural rather than a check: no
    submission module is on ``whiskeyjack_bot.pipeline``'s import path, so there is nothing
    here to call. ``CODEX_HANDOFF.md`` § "Required CLI entry points" asks that ``run`` never
    submit implicitly; a module that has no submission code is the strongest available form
    of that. Approval and submission stay separate commands (D23).

    ``--dry-run`` and ``--no-submit`` come from that same spec line. They are **assertions
    about the configuration, not overrides of it**: a flag that silently forced the safe
    value would let a config with ``dry_run: false`` pass a command line that reads as safe,
    and the operator would have been told the wrong thing about their own file. So each one
    refuses when the setting it names is not set, and omitting it asserts nothing -- the
    committed defaults are already the safe ones and this command cannot post either way.

    Prints the record's identity and hashes before exiting, for ``_run_approval``'s reason:
    the next command an operator runs is ``approve --forecast-sha256 <hash>``, and a hash
    they never saw printed is one they cannot bind an approval to.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.pipeline import ForecastRejected, PipelineError, run_replay
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

    if args.dry_run and not config.submission.dry_run:
        print("refused: --dry-run was passed but submission.dry_run is not set")
        return EXIT_REFUSED
    if args.no_submit and not config.submission.no_submit:
        print("refused: --no-submit was passed but submission.no_submit is not set")
        return EXIT_REFUSED

    connection = _open_existing_ledger(config.storage.sqlite_path)
    if connection is None:
        return EXIT_REFUSED
    try:
        try:
            result = run_replay(
                connection,
                config,
                question_id=args.question_id,
                attempt_id=args.attempt_id,
                snapshot=args.snapshot,
                now=datetime.now(tz=timezone.utc),
            )
        except ForecastRejected as exc:
            # Ordered before PipelineError, which it subclasses. The problems are
            # forecast.schema's sanitized list -- field paths and validator messages, never
            # the offending value -- and they are the whole account of why the reply was
            # rejected, so they are printed rather than summarized.
            print(f"rejected: {exc}")
            print(f"attempt:   {exc.attempt_id}")
            for problem in exc.problems:
                print(f"  - {problem}")
            return EXIT_REFUSED
        except PipelineError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(f"record:    {result.record_id}")
        print(
            f"question:  {result.question_id}  tournament: {result.tournament_id}  "
            f"version: {result.forecast_version}"
        )
        print(f"attempt:   {result.attempt_id} (replayed from {result.replayed_attempt_id})")
        print(f"research:  {len(result.retrieval_run_ids)} run(s), {result.source_count} source(s)")
        print(f"packet:    {result.research_packet_sha256}")
        # No `(none: ...)` fallback any more. `run_replay` refuses a run whose artifact was
        # not written -- round-1 finding 2 -- so reaching here means the path is set, and a
        # fallback for a state the pipeline cannot return would only ever mislead a reader
        # into thinking this command can produce a record without its evidence.
        print(f"artifact:  {result.raw_output_path}")
        print(f"hash:      {result.forecast_sha256}")
        print("status:    validated")
        print("submitted: no -- `run` never submits; approve and submit are separate commands")
        return EXIT_OK
    finally:
        connection.close()


def _run_replay(args: argparse.Namespace) -> int:
    """Re-derive one stored forecast from its saved model output (M1-406).

    **Corrected by T-903.** This docstring used to call itself "the entry point Codex's
    T-903 dry-run acceptance test needs: one command produces one validated record". It is
    half of that at most: this command *verifies* a record and writes nothing, so it cannot
    produce one. ``run`` is the half that produces it, and ``replay`` is what proves the
    hash reproduces afterwards. Left as written, the sentence would have told the next
    reader the gap was closed -- the failure mode a stale pointer in ``schema.py`` already
    cost M1-501 a blocking finding and a review round.

    Both hashes are printed whatever the verdict, and in that order, for
    ``_run_approval``'s reason: an operator acting on a replay needs to see the values it
    compared, not a word that summarizes them. A mismatch exits ``EXIT_REFUSED`` -- it is a
    finding about the ledger, and a command that exited 0 on one would be a check nothing
    in CI could gate.

    Nothing here contacts a provider. That is structural rather than promised: every module
    this imports is pinned by the import-graph test to reach no SDK and no HTTP client.
    """
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.forecast.record import ForecastRecordError
    from whiskeyjack_bot.forecast.replay import replay_forecast
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
            result = replay_forecast(connection, config, record_id=args.record_id)
        except ForecastRecordError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(f"record:    {result.record_id}")
        print(f"artifact:  {result.call.raw_output_path}")
        print(
            f"calls:     {result.call.model_invocations} invocation(s), "
            f"{result.raw_response_count} stored repl(y/ies), cost "
            + ("unknown" if result.call.cost_usd is None else f"{result.call.cost_usd:.6f} USD")
        )
        print(f"stored:    {result.stored_sha256}")
        print(f"replayed:  {result.replayed_sha256 or '(the stored reply no longer parses)'}")
        for problem in result.problems:
            print(f"  - {problem}")
        print(f"verdict:   {'match' if result.matches else 'MISMATCH'}")
        return EXIT_OK if result.matches else EXIT_REFUSED
    finally:
        connection.close()


def _run_export(args: argparse.Namespace) -> int:
    """Write the ledger out as derived JSONL or Parquet files (M1-604).

    The one command in this file that opens no ledger connection of its own. Every other
    ledger command goes through ``_open_existing_ledger``, which returns a *read-write*
    connection with migrations applied; using it here would mean an export could migrate
    the database it is exporting. :func:`export.export_ledger` takes the path and opens it
    read-only itself, so "never mutates the ledger" is a property of the call rather than a
    promise this handler makes on its behalf.

    ``--output`` defaults to a new UTC-timestamped directory under ``storage.export_root``
    -- a config field that has existed and been checked by ``verify-env`` since M1-601 with
    nothing reading it. The timestamp is in the directory name rather than the filenames so
    one export is one directory, and so a second export never collides with the first.
    Should it collide anyway (an explicit ``--output`` reused), the shared atomic writer
    refuses rather than overwriting: an export that silently replaced an earlier one would
    destroy the audit trail it exists to provide.

    Nothing here contacts a provider, and nothing writes to the ledger.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.export import ExportError, export_ledger
    from whiskeyjack_bot.ledger import LedgerError
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

    stamped = datetime.now(timezone.utc)
    destination = args.output
    if destination is None:
        # Colons are legal on POSIX but not on Windows, and a directory name is not a
        # timestamp anyone parses back, so compact the ISO form rather than embedding one.
        suffix = stamped.strftime("%Y%m%dT%H%M%SZ")
        destination = config.storage.export_root / f"{args.export_format}-{suffix}"

    try:
        result = export_ledger(
            config.storage.sqlite_path,
            destination,
            export_format=args.export_format,
            now=stamped,
        )
    except (ExportError, LedgerError) as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED

    print(f"ledger:    {config.storage.sqlite_path}")
    print(f"schema:    version {result.ledger_schema_version}")
    print(f"format:    {result.export_format}")
    print(f"output:    {result.destination}")
    print(f"tables:    {len(result.tables)} ({result.row_count} row(s) total)")
    for table in result.tables:
        print(f"  {table.row_count:>7} {table.name}")
    return EXIT_OK


def _run_report(args: argparse.Namespace) -> int:
    """Derive the attribution report dataset from the ledger (M5-804).

    ``_run_export``'s shape, for ``_run_export``'s reasons: no ledger connection of its own
    (:func:`report.write_report` takes the path and opens it read-only), ``--output``
    defaulting to a new UTC-timestamped directory under ``storage.export_root``, and a
    refusal rather than an overwrite if that directory already holds a report.

    What it prints is counts only. Nothing is summed across the groups of an axis -- on the
    overlapping axes that total would count records twice -- and the per-group numbers are
    in ``report.json``, where every cell carries its own ``n`` and small-sample flag.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.ledger import LedgerError
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.report import ReportError, write_report
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

    stamped = datetime.now(timezone.utc)
    destination = args.output
    if destination is None:
        destination = config.storage.export_root / f"report-{stamped.strftime('%Y%m%dT%H%M%SZ')}"

    try:
        result = write_report(config.storage.sqlite_path, destination, now=stamped)
    except (ReportError, LedgerError) as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED

    print(f"ledger:    {config.storage.sqlite_path}")
    print(f"schema:    version {result.ledger_schema_version}")
    print(f"output:    {result.destination}")
    print(f"records:   {result.records} ({result.excluded} excluded, {result.included} included)")
    for state, count in result.states:
        print(f"  {count:>7} {state}")
    for code, count in result.warnings:
        print(f"warning:   {code} ({count})")
    return EXIT_OK


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


def _open_readonly_ledger(path: Path) -> sqlite3.Connection | None:
    """Open an existing ledger read-only, or print why not and return ``None`` (M1-611).

    Same existence check as :func:`_open_existing_ledger`, for the same reason -- a
    mistyped ``--config`` must not report "no such record" against a ledger this command
    just discovered rather than the one an operator meant. What differs is the opener:
    :func:`ledger.connect_readonly` rather than :func:`ledger.open_verified_ledger`, because
    the latter is read-write and can apply a pending migration as a side effect of
    verifying the schema -- exactly what a read-only inspection command must not do.
    """
    from whiskeyjack_bot.ledger import LedgerError, connect_readonly

    try:
        exists = path.is_file()
    except OSError:
        print(f"cannot read the ledger database at {path}")
        return None
    if not exists:
        print(f"no ledger database at {path}; nothing has been recorded there yet")
        return None
    try:
        return connect_readonly(path)
    except LedgerError as exc:
        print(exc)
        return None


def _history_entry_detail(entry: HistoryEntry) -> str:
    """One line's worth of identifying detail for a merged canonical-history entry.

    Dispatches on ``entry.kind`` rather than checking each field for ``None``: exactly
    one payload field is set per :class:`show.HistoryEntry`'s own contract, so the match
    is total over the seven kinds :func:`show.merge_canonical_history` produces.
    """
    match entry.kind:
        case "approval":
            approval = entry.approval
            assert approval is not None
            return (
                f"{approval.decision} by {approval.actor}  "
                f"payload: {approval.payload_sha256 or '(none)'}"
            )
        case "submission_attempt":
            attempt = entry.submission_attempt
            assert attempt is not None
            return (
                f"attempt {attempt.attempt_id}  success: {attempt.success}  "
                f"http: {attempt.http_status}"
            )
        case "submission_verification":
            verification = entry.submission_verification
            assert verification is not None
            return f"attempt {verification.submission_attempt_id}  outcome: {verification.outcome}"
        case "lifecycle":
            event = entry.lifecycle_event
            assert event is not None
            return f"seq {event.event_seq}: {event.event_type}  {event.from_status} -> {event.to_status}"
        case "pre_forecast_failure":
            failure = entry.pre_forecast_failure
            assert failure is not None
            return f"{failure.event_type}  detail: {failure.detail_code}"
        case "resolution":
            resolution = entry.resolution
            assert resolution is not None
            return f"{resolution.kind}  outcome: {resolution.observation.outcome}"
        case "score":
            score = entry.score
            assert score is not None
            return f"{score.metric} = {score.value}{_score_source(score)}"


def _score_source(score: AnyStoredScore) -> str:
    """A platform score names what it is measured against and where it was read (M4-803)."""
    from whiskeyjack_bot.lifecycle import StoredPlatformScore

    if isinstance(score, StoredPlatformScore):
        return f"  vs {score.comparison_baseline}  source: {score.implementation_version}"
    return ""


def _print_show(view: RecordShow) -> None:
    summary = view.summary
    print(f"record:    {summary.record_id}")
    print(
        f"question:  {summary.question_id}  tournament: {summary.tournament_id}  "
        f"version: {summary.forecast_version}  type: {summary.question_type}"
    )
    print(f"status:    {summary.status}")
    print(f"hash:      {summary.forecast_sha256 or '(none stored)'}")
    print()

    if view.effective_approval is None:
        print("approval:  none in force")
    else:
        current = view.effective_approval
        print(f"approval:  {current.decision} by {current.actor} at {current.occurred_at_utc}")
        print(f"           payload: {current.payload_sha256 or '(none stored)'}")
    print(f"approval history ({len(view.approval_history)}):")
    for decision in view.approval_history:
        print(
            f"  - seq {decision.event_seq}: {decision.decision} by {decision.actor} "
            f"at {decision.occurred_at_utc}  payload: {decision.payload_sha256 or '(none)'}"
        )
    print()

    print(f"lifecycle history ({len(view.lifecycle_history)}):")
    for event in view.lifecycle_history:
        line = (
            f"  - seq {event.event_seq}: {event.event_type}  "
            f"{event.from_status} -> {event.to_status}  at {event.occurred_at_utc}"
        )
        if event.detail_code is not None:
            line += f"  detail: {event.detail_code}"
        if event.submission_attempt_id is not None:
            line += f"  attempt: {event.submission_attempt_id}"
        print(line)
    print()

    if view.unresolved_uncertainties:
        print(f"unresolved uncertainties ({len(view.unresolved_uncertainties)}):")
        for attempt_id in view.unresolved_uncertainties:
            print(
                f"  - attempt {attempt_id}: run `whiskeyjack-bot verify-submission "
                f"--record-id {summary.record_id} --attempt-id {attempt_id}`"
            )
    else:
        print("unresolved uncertainties: none")
    print()

    if view.standing_reservations:
        print(f"standing key reservations ({len(view.standing_reservations)}):")
        for reservation in view.standing_reservations:
            print(
                f"  - {reservation.reservation_id}  key: {reservation.idempotency_key}  "
                f"seq: {reservation.reservation_seq}  reserved: {reservation.reserved_at_utc}"
            )
    else:
        print("standing key reservations: none")
    print()

    if view.submission_attempts:
        print(f"submission attempts ({len(view.submission_attempts)}):")
        for attempt in view.submission_attempts:
            print(
                f"  - {attempt.attempt_id}  requested: {attempt.requested_at_utc}  "
                f"success: {attempt.success}  http: {attempt.http_status}  "
                f"refetch_outcome: {attempt.refetch_outcome}"
            )
    else:
        print("submission attempts: none")

    if view.submission_verifications:
        print(f"submission verifications ({len(view.submission_verifications)}):")
        for verification in view.submission_verifications:
            print(
                f"  - attempt {verification.submission_attempt_id}  "
                f"outcome: {verification.outcome}  observed: {verification.observed_at_utc}"
            )
    else:
        print("submission verifications: none")

    if view.resolution_history:
        print(f"resolution history ({len(view.resolution_history)}):")
        for resolution in view.resolution_history:
            print(
                f"  - seq {resolution.event_id}: {resolution.kind}  "
                f"outcome: {resolution.observation.outcome}  scorable: {resolution.scorable}  "
                f"observed: {resolution.observed_at_utc}"
            )
    else:
        print("resolution history: none")

    if view.score_history:
        print(f"score history ({len(view.score_history)}):")
        for score in view.score_history:
            print(
                f"  - seq {score.event_id}: {score.metric} = {score.value}"
                f"{_score_source(score)}  computed: {score.computed_at_utc}"
            )
    else:
        print("score history: none")

    if view.pre_forecast_failures:
        print(f"pre-forecast failures ({len(view.pre_forecast_failures)}):")
        for failure in view.pre_forecast_failures:
            print(
                f"  - seq {failure.event_seq}: {failure.event_type}  "
                f"detail: {failure.detail_code}  occurred: {failure.occurred_at_utc}"
            )
    else:
        print("pre-forecast failures: none")
    print()

    print(f"canonical history ({len(view.canonical_history)}):")
    for entry in view.canonical_history:
        print(f"  - {entry.occurred_at_utc}  [{entry.kind}]  {_history_entry_detail(entry)}")


def _run_show(args: argparse.Namespace) -> int:
    """Render one forecast record's ledger state, read-only (M1-611).

    Reads only: it opens the ledger through :func:`_open_readonly_ledger` (never
    :func:`_open_existing_ledger`, which is read-write) and calls nothing outside
    :mod:`whiskeyjack_bot.show`, which itself reaches no submission or provider module --
    see ``tests/unit/test_show.py``. Makes no network call and spends nothing.

    **Deliberately skips** :func:`logging_setup.configure_logging`, unlike every other
    handler in this file. That call creates the log directory and opens ``logging.file``
    for append -- a real filesystem write, and every other command's acceptance criterion
    tolerates that; this one's says "it writes nothing," unqualified. Safe to skip here
    because ``show`` reaches no code that would log anything sensitive to redact: its whole
    import graph is confirmed provider-free by the tests named above.
    """
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.research.allowlist import AllowlistError
    from whiskeyjack_bot.show import ShowError, assemble_show

    try:
        config = _load_verified_config(args.config)
    except ConfigError as exc:
        print(exc)
        return EXIT_CONFIG_INVALID
    except AllowlistError as exc:
        print(exc)
        return EXIT_ENV_MISSING if exc.is_filesystem_error else EXIT_CONFIG_INVALID

    connection = _open_readonly_ledger(config.storage.sqlite_path)
    if connection is None:
        return EXIT_REFUSED
    try:
        try:
            view = assemble_show(connection, args.record_id)
        except ShowError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        _print_show(view)
        return EXIT_OK
    finally:
        connection.close()


def _correct_costs(path: Path, *, apply: bool) -> int:
    """``tournament correct-costs`` (M1-348): no activation, no network, no config binding.

    The dry run opens the ledger with ``connect_readonly`` -- it must be safe to run against
    the live ledger while the worker polls -- and only ``--apply`` opens it for writing.
    """
    import json

    from whiskeyjack_bot.ledger import connect_readonly, open_verified_ledger
    from whiskeyjack_bot.tournament_state import correct_costs

    connection = open_verified_ledger(path) if apply else connect_readonly(path)
    try:
        report = correct_costs(connection, apply=apply)
    finally:
        connection.close()
    print(json.dumps(report.as_dict(applied=apply), indent=2))
    return 0


def _run_tournament(args: argparse.Namespace) -> int:
    import json
    from datetime import datetime
    from whiskeyjack_bot.config import load_config
    from whiskeyjack_bot.ledger import initialize_ledger, open_verified_ledger
    from whiskeyjack_bot.metaculus.client import build_poster
    from whiskeyjack_bot.tournament import run_once, status
    from whiskeyjack_bot.tournament_state import disable, enable

    try:
        config = load_config(args.config)
        from whiskeyjack_bot.logging_setup import configure_logging

        configure_logging(config)
        if args.tournament_command == "enable":
            initialize_ledger(config.storage.sqlite_path)
        if args.tournament_command == "correct-costs":
            return _correct_costs(config.storage.sqlite_path, apply=args.apply)
        connection = open_verified_ledger(config.storage.sqlite_path)
        try:
            if args.tournament_command == "enable":
                account = build_poster(config).get_current_user_id()
                identifier = enable(
                    connection,
                    config,
                    account_id=account,
                    project_id=args.project_id,
                    starts=datetime.fromisoformat(args.starts.replace("Z", "+00:00")),
                    ends=datetime.fromisoformat(args.ends.replace("Z", "+00:00")),
                    budget_usd=args.budget_usd,
                )
                print(
                    f"Activated policy {identifier} for account {account}, project {args.project_id}"
                )
            elif args.tournament_command == "disable":
                disable(connection)
                print("Tournament activation disabled")
            elif args.tournament_command == "reconcile-restored":
                from whiskeyjack_bot.restore import reconcile_restored

                print(
                    json.dumps(
                        reconcile_restored(connection, config, build_poster(config)), indent=2
                    )
                )
            elif args.tournament_command == "status":
                result = status(connection, config)
                print(json.dumps(result, indent=2))
                return 1 if result["refusal_reason"] else 0
            else:
                result = run_once(connection, config, question_id=args.question_id)
                print(json.dumps(result, indent=2))
                heartbeat = result.get("heartbeat") or {}
                return (
                    1
                    if result["refusal_reason"] or result["unresolved"] or heartbeat.get("failures")
                    else 0
                )
        finally:
            connection.close()
    except Exception as exc:
        # SDK exceptions may carry tokens and response bodies. Safe, stable type only.
        import logging

        from whiskeyjack_bot.tournament_state import ActivationInactive, TournamentError

        reason = str(exc) if isinstance(exc, TournamentError) else type(exc).__name__
        print(f"Tournament refused: {reason}")
        # M1-334: the same sanitized line into the JSONL log the operator tails. Before
        # this a refusal reached only stdout (the journal), so the tail went silent -- and a
        # silent tail reads exactly like a tournament between question batches. A disabled
        # or out-of-window activation is an ordinary resting state and logs as a warning;
        # anything else needs a person.
        logging.getLogger("whiskeyjack_bot.tournament").log(
            logging.WARNING if isinstance(exc, ActivationInactive) else logging.ERROR,
            "tournament refused: %s",
            reason,
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "tournament":
        return _run_tournament(args)
    if args.command == "verify-env":
        return _run_verify_env(args.config)
    if args.command == "init-ledger":
        return _run_init_ledger(args)
    if args.command == "questions":
        if args.questions_command != "fetch":
            parser.parse_args(["questions", "--help"])
            return 2
        return _run_questions_fetch(args)
    if args.command == "approve":
        return _run_approval(args, "approved")
    if args.command == "reject":
        return _run_approval(args, "rejected")
    if args.command == "submit":
        return _run_submit(args)
    if args.command == "verify-submission":
        return _run_verify_submission(args)
    if args.command == "ingest-resolutions":
        return _run_ingest_resolutions(args)
    if args.command == "score":
        return _run_score(args)
    if args.command == "run":
        return _run_run(args)
    if args.command == "run-replay":
        return _run_run_replay(args)
    if args.command == "release-key":
        return _run_release_key(args)
    if args.command == "reconcile-submission":
        return _run_reconcile_submission(args)
    if args.command == "unrecorded-posts":
        return _run_unrecorded_posts(args)
    if args.command == "replay":
        return _run_replay(args)
    if args.command == "show":
        return _run_show(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "report":
        return _run_report(args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
