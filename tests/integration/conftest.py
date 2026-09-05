"""Fixtures for the T-902 mocked-integration tier.

Thin wrappers over ``fake_platform``. The network guard needs nothing here: pytest's
``addopts`` carries ``--disable-socket --allow-hosts=127.0.0.0/8,::1`` process-wide and
``tests/conftest.py`` installs an autouse DNS block, so both apply to this directory
already. ``tests/unit/conftest.py``'s ``block_network`` is the superseded third copy and is
deliberately **not** repeated here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fake_platform import (
    BINARY_POST_ID,
    BINARY_QUESTION_ID,
    LIVE_SUBMISSION_FLAGS,
    OCCURRED,
    RESEARCH_TIMESTAMP,
    RUN_ID,
    config_data,
)

from tests.unit.records import CALIBRATION, FORECAST_CONFIG
from tests.unit.test_submission_live import _draft, _generation, _question, _response

from whiskeyjack_bot.approval import approve
from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.forecast.store import append_forecast_version
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import record_validation, transaction

FAKE_TOKEN = "fake-metaculus-token-for-integration-0001"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    """The committed defaults: submission off, paced at zero, storage under ``tmp_path``."""
    return validate_config_data(config_data(tmp_path))


@pytest.fixture()
def live_config(tmp_path: Path) -> AppConfig:
    """All three submission flags flipped, which is what a post requires."""
    return validate_config_data(config_data(tmp_path, **LIVE_SUBMISSION_FLAGS))


@pytest.fixture()
def live_config_file(tmp_path: Path) -> Path:
    """The same live config on disk, for the tests that drive ``main(["submit", ...])``."""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(config_data(tmp_path, **LIVE_SUBMISSION_FLAGS)), encoding="utf-8"
    )
    return path


@pytest.fixture()
def metaculus_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """``build_client`` reads the token at construction time, so setting it is enough."""
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    return FAKE_TOKEN


@pytest.fixture()
def ledger(config: AppConfig) -> Iterator[sqlite3.Connection]:
    """A real ledger at the configured path, built by the real migrations."""
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    connection = connect(config.storage.sqlite_path)
    try:
        yield connection
    finally:
        connection.close()


def _seed_research(conn: sqlite3.Connection) -> None:
    """The FK ``forecast_records.retrieval_run_id`` points at. Same shape as the unit tier's."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO research_runs "
            "(retrieval_run_id, provider, started_at_utc, created_at_utc, question_id) "
            "VALUES (?, 'exa', ?, ?, ?)",
            (RUN_ID, RESEARCH_TIMESTAMP, RESEARCH_TIMESTAMP, BINARY_QUESTION_ID),
        )


def build_validated_record(conn: sqlite3.Connection) -> str:
    """A validated, **unapproved** forecast record on the committed fixture's identity.

    The identity matters and is the reason this is not
    ``tests/unit/test_submission_live.py``'s ``approved`` fixture reused verbatim: that one
    builds on ``(123, 456)``, and the refetch here is a real API post parsed by the SDK from
    ``tests/fixtures/api_posts/binary_post.json``, whose ids are ``(91001, 90001)``.
    ``MetaculusSubmissionGateway._require_matching_identity`` compares the two, so a record
    on borrowed ids would be refused for the wrong reason and every post test below would
    pass for a reason unrelated to what it claims.
    """
    _seed_research(conn)
    draft = _draft(
        question=_question(question_id=BINARY_QUESTION_ID, post_id=BINARY_POST_ID),
        generation=_generation(forecast=_response(question_id=BINARY_QUESTION_ID)),
    )
    record = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=draft)
    record_validation(conn, record_id=record.record_id, occurred_at=OCCURRED)
    return str(record.record_id)


@pytest.fixture()
def validated_record(ledger: sqlite3.Connection) -> tuple[sqlite3.Connection, str]:
    """Validated but never approved -- the precondition the approval gate is about."""
    return ledger, build_validated_record(ledger)


@pytest.fixture()
def approved_record(ledger: sqlite3.Connection) -> tuple[sqlite3.Connection, str]:
    """Ready to post: validated, then approved through the real ``approve`` command path."""
    record_id = build_validated_record(ledger)
    approve(
        ledger,
        record_id=record_id,
        actor="chris",
        occurred_at=OCCURRED,
        calibration=CALIBRATION,
    )
    return ledger, record_id
