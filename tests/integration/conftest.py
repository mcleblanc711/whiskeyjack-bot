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
from fake_platform import LIVE_SUBMISSION_FLAGS, config_data

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger

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
