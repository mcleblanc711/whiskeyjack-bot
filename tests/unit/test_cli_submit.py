"""M2-704: the `submit` and `verify-submission` commands at the CLI boundary.

What is under test is the command layer's own behaviour -- required arguments, what is
printed *before* anything is posted, the exit code a refusal produces, and that the
committed configuration cannot post. The submission semantics themselves are
`tests/unit/test_submission_live.py`.

The poster is replaced at `whiskeyjack_bot.metaculus.client.build_poster`, which is the one
construction point the command goes through. Nothing here touches a network: the suite
blocks sockets, and `submission_live` imports no HTTP client at all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.approval import approve
from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_ENV_MISSING, EXIT_OK
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import current_status, record_validation
from whiskeyjack_bot.metaculus.client import MissingCredentialError

from tests.unit.test_submission_live import (  # noqa: F401 - fixtures reused deliberately
    BINARY_PAYLOAD,
    PAYLOAD_SHA,
    NEW_START,
    OCCURRED,
    PROBABILITY,
    QUESTION_ID,
    RUN_ID,
    TIMESTAMP,
    FakePoster,
    FakeQuestion,
    _binary_values,
    _draft,
    _entry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A config whose flags permit a live post, with every path under tmp_path."""
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
    data["submission"].update({"enabled": True, "dry_run": False, "no_submit": False})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture()
def committed_config_file(config_file: Path) -> Path:
    """The same config with the shipped submission flags, which must not post."""
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["submission"].update({"enabled": False, "dry_run": True, "no_submit": True})
    path = config_file.parent / "committed.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture()
def record_id(config_file: Path) -> str:
    """One approved forecast record, in a ledger at the configured path."""
    from whiskeyjack_bot.forecast.store import append_forecast_version
    from whiskeyjack_bot.lifecycle import transaction

    database = Path(
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"]
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(database)
    conn = connect(database)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
                "started_at_utc, created_at_utc) VALUES (?, 'exa', ?, ?, ?)",
                (RUN_ID, QUESTION_ID, TIMESTAMP, TIMESTAMP),
            )
        record = append_forecast_version(conn, draft=_draft())
        record_validation(conn, record_id=record.record_id, occurred_at=OCCURRED)
        approve(
            conn,
            record_id=record.record_id,
            actor="chris",
            occurred_at=OCCURRED,
            payload_sha256=PAYLOAD_SHA,
        )
        return record.record_id
    finally:
        conn.close()


@pytest.fixture()
def payload_file(tmp_path: Path) -> Path:
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(BINARY_PAYLOAD), encoding="utf-8")
    return path


def _install(monkeypatch: pytest.MonkeyPatch, poster: Any) -> None:
    monkeypatch.setattr("whiskeyjack_bot.metaculus.client.build_poster", lambda _config: poster)


def test_submit_posts_once_and_prints_what_it_recorded(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    poster = FakePoster()
    _install(monkeypatch, poster)
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    captured = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert poster.posts == 1
    assert "result:    submitted" in captured
    assert "artifact:" in captured
    assert f"record:    {record_id}" in captured


def test_submit_prints_the_record_and_the_payload_digest_before_posting(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An operator must be able to see what is about to happen, `approve`'s shape.

    The ordering is the claim: the identity and the digest are printed even when the post
    itself is refused, so a refusal is still a description of what was being attempted.
    """
    poster = FakePoster(before=FakeQuestion(state="closed"))
    _install(monkeypatch, poster)
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    captured = capsys.readouterr().out
    assert exit_code == EXIT_REFUSED
    assert poster.posts == 0
    assert "payload:   sha256 " in captured
    assert "status:    approved" in captured
    assert "refused:" in captured


def test_submit_without_a_payload_file_posts_the_payload_the_record_derives(
    config_file: Path,
    record_id: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M2-707 made `--payload-file` optional, and this is why that is the safe direction.

    Now that an approval binds to a payload digest, the payload the record derives is the
    only one that can reach a post at all -- so requiring an operator to hand-write it
    (a 201-point CDF, for a numeric question) would have made the command undrivable
    without making anything safer. Omitted, the command derives it, and the digest it prints
    is the one the approval holds: the post goes through, which is the assertion, because a
    derivation that disagreed with `approve`'s would fail at the key seam instead.
    """
    poster = FakePoster()
    _install(monkeypatch, poster)
    exit_code = main(["submit", "--config", str(config_file), "--record-id", record_id])
    captured = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert poster.posts == 1
    assert f"payload:   sha256 {PAYLOAD_SHA} (derived)" in captured
    assert "result:    submitted" in captured


def test_a_supplied_payload_the_approval_never_authorized_is_refused_before_any_post(
    config_file: Path,
    record_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """*"A submission payload that does not derive from the approved forecast is refused
    before any post"* -- the acceptance criterion, from the command line.

    A well-formed binary payload for the same question, differing only in the probability.
    Before M2-707 this posted: the forecast hash was unchanged, so the approval was still in
    force and the changed payload merely produced a different idempotency key. `poster.posts`
    is the assertion that matters -- zero is the only way to show the gate ran in front of
    the post rather than after it.
    """
    unauthorized = tmp_path / "unauthorized.json"
    unauthorized.write_text(
        json.dumps({**BINARY_PAYLOAD, "probability_yes": PROBABILITY + 0.1}), encoding="utf-8"
    )
    poster = FakePoster()
    _install(monkeypatch, poster)
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(unauthorized),
        ]
    )
    captured = capsys.readouterr().out
    assert exit_code == EXIT_REFUSED
    assert poster.posts == 0
    assert "(from file)" in captured
    assert "not the one the approval in force authorized" in captured


def test_the_committed_config_refuses_before_a_token_is_even_read(
    committed_config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shipped configuration cannot reach the network, whatever the validator allows.

    `build_poster` is replaced with something that raises, so the assertion is that the
    config gate runs **first**: an operator running `submit` against the committed
    configuration must be told submission is off, not that METACULUS_TOKEN is missing.
    """

    def never(_config: Any) -> Any:
        raise AssertionError("a poster must not be built when submission is off")

    monkeypatch.setattr("whiskeyjack_bot.metaculus.client.build_poster", never)
    exit_code = main(
        [
            "submit",
            "--config",
            str(committed_config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    assert exit_code == EXIT_REFUSED
    assert "live submission is off" in capsys.readouterr().out


def test_the_committed_config_makes_no_network_call(
    committed_config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    poster = FakePoster()
    _install(monkeypatch, poster)
    exit_code = main(
        [
            "submit",
            "--config",
            str(committed_config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    assert exit_code == EXIT_REFUSED
    assert poster.posts == 0
    assert poster.fetches == 0
    assert "live submission is off" in capsys.readouterr().out


def test_a_missing_token_exits_env_missing_without_posting(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(_config: Any) -> Any:
        raise MissingCredentialError("METACULUS_TOKEN")

    monkeypatch.setattr("whiskeyjack_bot.metaculus.client.build_poster", refuse)
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    captured = capsys.readouterr().out
    assert exit_code == EXIT_ENV_MISSING
    assert "METACULUS_TOKEN" in captured


@pytest.mark.parametrize(
    ("contents", "needle"),
    [
        ("not json at all", "not valid JSON"),
        ('["a list"]', "JSON object"),
    ],
)
def test_a_bad_payload_file_is_refused_with_its_path(
    config_file: Path,
    record_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    contents: str,
    needle: str,
) -> None:
    """Paths are the settled M1-401 carve-out and are rendered; contents never are."""
    bad = tmp_path / "bad.json"
    bad.write_text(contents, encoding="utf-8")
    poster = FakePoster()
    _install(monkeypatch, poster)
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(bad),
        ]
    )
    captured = capsys.readouterr().out
    assert exit_code == EXIT_REFUSED
    assert needle in captured
    assert str(bad) in captured
    assert poster.posts == 0


def test_a_missing_payload_file_is_refused_before_the_ledger_is_opened(
    config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.json"
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            "rec-1",
            "--payload-file",
            str(missing),
        ]
    )
    assert exit_code == EXIT_REFUSED
    assert "cannot read the payload file" in capsys.readouterr().out


def test_an_uncertain_submission_tells_the_operator_the_next_command(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dead end is not an acceptable state to leave an operator in."""
    _install(monkeypatch, FakePoster(after=FakeQuestion(history=[])))
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    captured = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "result:    submission_uncertain" in captured
    assert "verify-submission" in captured
    assert "--attempt-id wjlive-1-" in captured


def test_verify_submission_resolves_the_uncertainty_the_submit_left(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole operator loop, through the commands rather than the library."""
    _install(monkeypatch, FakePoster(after=FakeQuestion(history=[])))
    assert (
        main(
            [
                "submit",
                "--config",
                str(config_file),
                "--record-id",
                record_id,
                "--payload-file",
                str(payload_file),
            ]
        )
        == EXIT_OK
    )
    printed = capsys.readouterr().out
    attempt_id = next(
        line.split()[-1] for line in printed.splitlines() if line.startswith("attempt:")
    )

    later = FakePoster(after=FakeQuestion(history=[_entry(NEW_START, _binary_values(PROBABILITY))]))
    later.posts = 1
    _install(monkeypatch, later)
    exit_code = main(
        [
            "verify-submission",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--attempt-id",
            attempt_id,
        ]
    )
    captured = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "result:    submission_confirmed" in captured
    assert later.posts == 1, "verify-submission must never post"

    database = Path(
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"]
    )
    conn = connect(database)
    try:
        assert current_status(conn, record_id) == "submitted"
    finally:
        conn.close()


def test_submit_against_a_missing_ledger_refuses(
    config_file: Path, payload_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mistyped --config must not mint an empty database and report a false absence."""
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            "rec-1",
            "--payload-file",
            str(payload_file),
        ]
    )
    assert exit_code == EXIT_REFUSED
    assert "no ledger database at" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["submit", "verify-submission", "release-key"])
def test_the_commands_require_their_arguments(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([command])
    assert excinfo.value.code == 2


# ── M2-708: the `release-key` command, and the way out `submit` now prints ────


def _ledger_at(config_file: Path) -> Any:
    database = Path(
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"]
    )
    return connect(database)


def _hold_key(config_file: Path, record_id: str, payload: Any = None) -> str:
    """Leave a reservation standing, the way a killed process would.

    Written through the real writer rather than by raw SQL: what the command has to cope
    with is the state `submit` actually leaves, and a hand-built row could differ from it
    in a way no test would notice.
    """
    from whiskeyjack_bot.submission import reserve_submission_key, submission_key_for_record
    from whiskeyjack_bot.submission_gateway import payload_sha256

    conn = _ledger_at(config_file)
    try:
        digest = payload_sha256(dict(BINARY_PAYLOAD) if payload is None else payload)
        key = submission_key_for_record(conn, record_id, request_payload_sha256=digest)
        reservation = reserve_submission_key(
            conn,
            record_id=record_id,
            idempotency_key=key,
            reserved_at=OCCURRED,
        )
        return reservation.reservation_id
    finally:
        conn.close()


def _live_reservations(config_file: Path, record_id: str) -> int:
    conn = _ledger_at(config_file)
    try:
        return int(
            conn.execute(
                "SELECT count(*) FROM submission_key_reservations r WHERE "
                "r.forecast_record_id = ? AND NOT EXISTS (SELECT 1 FROM "
                "submission_key_releases x WHERE x.reservation_id = r.reservation_id)",
                (record_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_release_key_frees_a_standing_reservation(
    config_file: Path, record_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    reservation_id = _hold_key(config_file, record_id)
    assert _live_reservations(config_file, record_id) == 1

    exit_code = main(
        [
            "release-key",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--released-by",
            "chris",
            "--note",
            "checked the platform; nothing there",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert reservation_id in out
    assert "operator_abandoned" in out
    # The warning is printed *before* the write, because this command is reached long
    # after the message that explains when releasing is the wrong thing to do.
    assert "do not release" in out
    assert _live_reservations(config_file, record_id) == 0


def test_release_key_records_the_person_and_the_note(config_file: Path, record_id: str) -> None:
    """The release is an attribution claim, so it has to be stored as one."""
    _hold_key(config_file, record_id)
    main(
        [
            "release-key",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--released-by",
            "chris",
            "--note",
            "checked the platform",
        ]
    )
    conn = _ledger_at(config_file)
    try:
        row = conn.execute(
            "SELECT reason, released_by, note FROM submission_key_releases"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("operator_abandoned", "chris", "checked the platform")


def test_release_key_refuses_when_nothing_is_standing(
    config_file: Path, record_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A no-op that reported success would tell an operator their key was freed."""
    exit_code = main(
        [
            "release-key",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--released-by",
            "chris",
        ]
    )
    assert exit_code == EXIT_REFUSED
    assert "nothing to release" in capsys.readouterr().out


def test_release_key_lists_rather_than_guesses_between_two_reservations(
    config_file: Path, record_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`010` constrains one *key* to one live reservation, not one record.

    Two payloads for one record are two keys and so two live claims, and only the operator
    knows which submission they went and checked. Picking one would be an invisible guess
    about which post may have landed.
    """
    first = _hold_key(config_file, record_id)
    second = _hold_key(config_file, record_id, payload={**BINARY_PAYLOAD, "probability_yes": 0.42})
    assert first != second
    assert _live_reservations(config_file, record_id) == 2

    exit_code = main(
        [
            "release-key",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--released-by",
            "chris",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == EXIT_REFUSED
    assert "--reservation-id" in out
    assert first in out and second in out
    # Nothing was released: a refusal that had picked one would be the defect.
    assert _live_reservations(config_file, record_id) == 2


def test_release_key_releases_the_named_one_of_two(
    config_file: Path, record_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control for the test above: with the ambiguity resolved, it acts."""
    first = _hold_key(config_file, record_id)
    _hold_key(config_file, record_id, payload={**BINARY_PAYLOAD, "probability_yes": 0.42})

    exit_code = main(
        [
            "release-key",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--released-by",
            "chris",
            "--reservation-id",
            first,
        ]
    )
    assert exit_code == EXIT_OK
    assert first in capsys.readouterr().out
    assert _live_reservations(config_file, record_id) == 1


def test_release_key_refuses_a_reservation_id_that_is_not_standing(
    config_file: Path, record_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _hold_key(config_file, record_id)
    exit_code = main(
        [
            "release-key",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--released-by",
            "chris",
            "--reservation-id",
            "wjres-" + "0" * 32,
        ]
    )
    assert exit_code == EXIT_REFUSED
    assert "does not name a standing reservation" in capsys.readouterr().out
    assert _live_reservations(config_file, record_id) == 1


def test_release_key_needs_a_ledger(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No `record_id` fixture, so no database was ever created."""
    exit_code = main(
        [
            "release-key",
            "--config",
            str(config_file),
            "--record-id",
            "rec-nothing",
            "--released-by",
            "chris",
        ]
    )
    assert exit_code == EXIT_REFUSED
    assert "no ledger database at" in capsys.readouterr().out


def test_submit_prints_the_way_out_when_a_reservation_is_standing(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other half of the promise: a blocked operator is told what to do.

    The library refusal deliberately names no key -- it is derived from a payload hash,
    and echoing it would let a caller confirm a guess about stored content -- so without
    this the operator learns they are blocked and nothing else.
    """
    _hold_key(config_file, record_id)
    poster = FakePoster()
    _install(monkeypatch, poster)

    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == EXIT_REFUSED
    assert poster.posts == 0
    assert "reserved by a submission that has not finished" in out
    assert f"release-key --record-id {record_id}" in out


def test_submit_prints_no_release_hint_when_it_refuses_for_another_reason(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control: a closed question releases its own key on the way out, so there is
    nothing standing and the hint must not appear. Printing it unconditionally would send
    an operator to release a reservation that does not exist."""
    _install(monkeypatch, FakePoster(before=FakeQuestion(state="closed")))
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == EXIT_REFUSED
    assert "not open" in out
    assert "release-key" not in out
    # The header too, not just the command line. Without the emptiness guard the helper
    # still prints "a key reservation is standing ... (0)" and then loops over nothing --
    # no `release-key` line, and an operator told to release a claim that does not exist.
    assert "key reservation is standing" not in out
