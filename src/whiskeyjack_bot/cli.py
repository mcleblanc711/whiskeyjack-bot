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

    submit = subparsers.add_parser(
        "submit",
        help="post one approved forecast to Metaculus and verify it by refetch",
    )
    submit.add_argument("--config", default="config.yaml", type=Path)
    submit.add_argument("--record-id", required=True, help="the approved forecast record to post")
    submit.add_argument(
        "--payload-file",
        required=True,
        type=Path,
        help=(
            "JSON file holding the Metaculus request payload: question_type plus one of "
            "probability_yes / continuous_cdf / probability_yes_per_category"
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

    run = subparsers.add_parser(
        "run",
        help=(
            "forecast one saved question from replayed research and a saved model reply; "
            "makes no provider call and never submits"
        ),
    )
    run.add_argument("--config", default="config.yaml", type=Path)
    run.add_argument(
        "--question-id", required=True, type=int, help="the question in the snapshot to forecast"
    )
    run.add_argument(
        "--snapshot", required=True, type=Path, help="the saved question snapshot to load from"
    )
    run.add_argument(
        "--attempt-id",
        required=True,
        help=(
            "the saved attempt whose model reply to replay; the record this writes is "
            "stamped with a freshly minted attempt id of its own"
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

    replay = subparsers.add_parser(
        "replay",
        help="re-derive a stored forecast from its saved model output; makes no API call",
    )
    replay.add_argument("--config", default="config.yaml", type=Path)
    replay.add_argument("--record-id", required=True, help="the forecast record to replay")
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


def _run_submit(args: argparse.Namespace) -> int:
    """Post one approved forecast, and print what was recorded (M2-704).

    **This is the only command in the tree that can cause a live Metaculus post**, and it
    is arranged so an operator sees what is about to happen before it does: the record's
    identity, its derived status, the hash the approval binds to, the payload digest and
    the derived idempotency key are all printed first. That is ``approve``'s shape and it
    is here for the same reason -- a submission whose payload the operator never saw
    described is an attribution claim with nothing behind it.

    ``--payload-file`` rather than a built payload because there is no payload builder:
    M1-502/M1-503 are ``Not Started``. M2-703's notes anticipated exactly this and said the
    file argument lands here.

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

    payload = _read_payload_file(args.payload_file)
    if payload is None:
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
        try:
            digest = payload_sha256(payload)
        except SubmissionError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(f"payload:   sha256 {digest}")

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
        return EXIT_OK
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
            "post did land -- do not release; resolve that attempt instead."
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


def _run_run(args: argparse.Namespace) -> int:
    """Forecast one saved question from replayed research and a saved reply (T-903).

    The command T-903's criterion is about: *"one command, one saved question, research +
    model replay -> one complete validated ledger record, zero provider calls, zero
    submission calls, reproducible forecast hash."* The last clause is ``replay
    --record-id`` run afterwards on the record this prints.

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
    if args.command == "submit":
        return _run_submit(args)
    if args.command == "verify-submission":
        return _run_verify_submission(args)
    if args.command == "run":
        return _run_run(args)
    if args.command == "release-key":
        return _run_release_key(args)
    if args.command == "replay":
        return _run_replay(args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
