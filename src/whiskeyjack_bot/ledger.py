"""SQLite attribution-ledger schema and access layer (M1-601).

The ledger is the v1 source of truth (decision D16): an append-only, replayable
record of every forecast, its evidence, approvals, submission attempts,
resolutions and scores. This module owns database connections and migration
application only; row-writing helpers, forecast versioning and the append-only
enforcement mechanism land with M1-602/M1-603.

Migrations live inside the package (:mod:`whiskeyjack_bot.migrations`) rather
than at the repository root shown in the handoff's proposed tree, so they ship
in the wheel and load via ``importlib.resources`` regardless of install layout.

Error hygiene follows ``ConfigError``/``SnapshotError``: a :class:`LedgerError`
never echoes stored row values, and wrapped raises use ``from None`` so an
underlying database exception cannot reprint a value through its cause chain.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

LEDGER_SCHEMA_VERSION = 1

_MIGRATIONS_PACKAGE = "whiskeyjack_bot.migrations"
_MIGRATION_NAME_RE = re.compile(r"^(\d+)_.*\.sql$")
_BUSY_TIMEOUT_MS = 5000


class LedgerError(Exception):
    """The ledger database cannot be opened or migrated.

    Same hygiene rule as ``ConfigError``: the message never echoes stored
    values, and wrapped raises use ``from None`` so a mistakenly stored secret
    cannot surface through the underlying exception's text or traceback.
    """


def connect(path: Path) -> sqlite3.Connection:
    """Open the ledger with WAL, foreign keys and explicit-transaction mode.

    Purely local file I/O: no network access on any path through here.
    """
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error:
        # from None: the underlying error can name the file; wrap it and keep
        # the cause chain from reprinting anything about the target.
        raise LedgerError(f"cannot open ledger database at {path}") from None
    try:
        conn.row_factory = sqlite3.Row
        # Autocommit mode: DDL and migration bookkeeping run inside explicit
        # BEGIN/COMMIT blocks (spec: explicit transactions), while the pragmas
        # below run outside any transaction as SQLite requires.
        conn.isolation_level = None
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        conn.close()
        raise LedgerError(f"cannot open ledger database at {path}") from None
    return conn


def initialize_ledger(path: Path) -> int:
    """Create or upgrade the ledger schema; return the applied schema version.

    Idempotent: migrations already recorded in ``schema_migrations`` are
    skipped, so re-running is a no-op that does not error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        applied = _applied_versions(conn)
        for version, sql, checksum in _load_migrations():
            if version not in applied:
                _apply_migration(conn, version, sql, checksum)
        return _current_version(conn)
    finally:
        conn.close()


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if present is None:
        return set()
    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _load_migrations() -> list[tuple[int, str, str]]:
    migrations: list[tuple[int, str, str]] = []
    for entry in files(_MIGRATIONS_PACKAGE).iterdir():
        match = _MIGRATION_NAME_RE.match(entry.name)
        if match is None:
            continue
        data = entry.read_bytes()
        migrations.append(
            (int(match.group(1)), data.decode("utf-8"), hashlib.sha256(data).hexdigest())
        )
    migrations.sort(key=lambda item: item[0])
    return migrations


def _apply_migration(conn: sqlite3.Connection, version: int, sql: str, checksum: str) -> None:
    applied_at = datetime.now(tz=timezone.utc).isoformat()
    try:
        conn.execute("BEGIN")
        for statement in _statements(sql):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at_utc, checksum) VALUES (?, ?, ?)",
            (version, applied_at, checksum),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        # version is a filename-derived integer, never a stored value.
        raise LedgerError(f"failed to apply ledger migration {version}") from None


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [chunk.strip() for chunk in body.split(";") if chunk.strip()]
