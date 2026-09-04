"""M2-712: the `init-ledger` command at the CLI boundary.

Before this command existed, no subcommand ever called `ledger.initialize_ledger()`, so a
fresh checkout had no first-time path past `_open_existing_ledger`'s "no ledger database"
refusal. What's under test here is the command layer's own wiring -- that it lands a fresh
database at the current schema version, that a second run is a true no-op, and that it
refuses without creating anything when the target directory cannot be made. It deliberately
does not re-test `initialize_ledger()`'s own idempotency/checksum machinery, which
`tests/unit/test_ledger.py` already covers at the module level.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_file(tmp_path: Path, sqlite_path: Path) -> Path:
    """A valid config whose data paths live under tmp_path, mirroring test_cli_replay's shape."""
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(sqlite_path)
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _schema_migrations_rows(db: Path) -> list[tuple[int, str, str]]:
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT version, applied_at_utc, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    return [(row[0], row[1], row[2]) for row in rows]


def test_fresh_ledger_is_created_and_lands_at_current_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "data" / "ledger.db"
    config_file = _config_file(tmp_path, db)

    assert not db.exists()
    assert main(["init-ledger", "--config", str(config_file)]) == EXIT_OK

    assert db.is_file()
    conn = connect(db)
    try:
        (version,) = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
    finally:
        conn.close()
    assert version == LEDGER_SCHEMA_VERSION

    out = capsys.readouterr().out
    assert str(db) in out
    assert str(LEDGER_SCHEMA_VERSION) in out


def test_second_invocation_is_a_noop(tmp_path: Path) -> None:
    db = tmp_path / "data" / "ledger.db"
    config_file = _config_file(tmp_path, db)

    assert main(["init-ledger", "--config", str(config_file)]) == EXIT_OK
    first_rows = _schema_migrations_rows(db)

    assert main(["init-ledger", "--config", str(config_file)]) == EXIT_OK
    second_rows = _schema_migrations_rows(db)

    # No new rows and no re-insertion: applied_at_utc/checksum for every migration are
    # untouched, proving the second call verified and did not re-apply anything.
    assert second_rows == first_rows


def test_refuses_without_creating_anything_when_parent_cannot_be_made(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    db = blocked / "data" / "ledger.db"
    config_file = _config_file(tmp_path, db)

    assert main(["init-ledger", "--config", str(config_file)]) == EXIT_REFUSED
    assert not (blocked / "data").exists()
    assert not db.exists()
