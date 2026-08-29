"""Fixtures over the shared replay scenario in :mod:`tests.scenario` (T-903)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from scenario import FAKE_VALUES, Seed, config_data, seed_scenario, write_config
from whiskeyjack_bot.ledger import connect


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    return write_config(tmp_path, config_data(tmp_path))


@pytest.fixture()
def seed(config_file: Path) -> Seed:
    return seed_scenario(config_file)


@pytest.fixture()
def ledger(seed: Seed) -> Iterator[sqlite3.Connection]:
    conn = connect(seed.config.storage.sqlite_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def fake_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in FAKE_VALUES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    return dict(FAKE_VALUES)
