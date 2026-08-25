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
        approve(conn, record_id=record.record_id, actor="chris", occurred_at=OCCURRED)
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


@pytest.mark.parametrize("command", ["submit", "verify-submission"])
def test_the_commands_require_their_arguments(command: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([command])
    assert excinfo.value.code == 2
