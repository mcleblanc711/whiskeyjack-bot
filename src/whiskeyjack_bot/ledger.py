"""SQLite attribution-ledger schema and access layer (M1-601).

The ledger is the v1 source of truth (decision D16): an append-only-by-policy,
replayable record of every forecast, its evidence, approvals, submission
attempts, resolutions and scores. This module owns database connections and
migration application only; row-writing helpers, forecast versioning and the
database-level append-only enforcement (UPDATE/DELETE-blocking triggers) land
with M1-602/M1-603.

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
        journal_mode_row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        conn.close()
        raise LedgerError(f"cannot open ledger database at {path}") from None
    # PRAGMA journal_mode returns the mode actually in force. A filesystem/VFS that
    # cannot support WAL -- or a special path such as :memory: -- silently keeps a
    # different mode, which would break the ledger's durability assumptions, so fail
    # loudly rather than run in a mode the caller did not ask for.
    journal_mode = str(journal_mode_row[0]).lower() if journal_mode_row is not None else ""
    if journal_mode != "wal":
        conn.close()
        raise LedgerError(f"ledger database at {path} does not support WAL journal mode")
    return conn


def initialize_ledger(path: Path) -> int:
    """Create or upgrade the ledger schema; return the applied schema version.

    Idempotent: migrations already recorded in ``schema_migrations`` are
    skipped (after a checksum re-check), so re-running is a no-op that does not
    error. Fails safely on schema drift, duplicate migration numbers, or a
    database written by a newer build.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # from None: the OSError can name parent paths; wrap it with a constant
        # message so nothing about the target filesystem is reprinted.
        raise LedgerError(f"cannot create ledger directory for {path}") from None
    conn = connect(path)
    try:
        migrations = _load_migrations()
        applied = _applied_migrations(conn)
        _reject_newer_database(applied, migrations)
        for version, sql, checksum in migrations:
            if version in applied:
                _verify_checksum(version, applied[version], checksum)
                continue
            _apply_migration(conn, version, sql, checksum)
        return _current_version(conn)
    finally:
        conn.close()


def _applied_migrations(conn: sqlite3.Connection) -> dict[int, str]:
    """Return already-applied migrations as ``{version: stored_checksum}``.

    A malformed ``schema_migrations`` table (missing columns, or a non-integer
    version) is treated as an opening failure rather than allowed to raise a raw
    exception whose text could echo a stored value.
    """
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if present is None:
        return {}
    try:
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        return {int(row[0]): str(row[1]) for row in rows}
    except (sqlite3.Error, TypeError, ValueError):
        # from None: a malformed row (non-integer/NULL version, absent column) can
        # carry arbitrary stored bytes through the underlying message or traceback.
        raise LedgerError(
            "ledger schema_migrations table is malformed "
            "(detail withheld: it can echo stored values)"
        ) from None


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # from None: same hygiene as _applied_migrations -- never echo the value.
        raise LedgerError(
            "ledger schema_migrations contains a non-integer version "
            "(detail withheld: it can echo stored values)"
        ) from None


def _verify_checksum(version: int, stored: str, expected: str) -> None:
    if stored != expected:
        # Constant message: never echo the stored or expected checksum. A mismatch
        # means the packaged migration changed after it was applied (schema drift).
        raise LedgerError(
            f"ledger migration {version} does not match the checksum recorded when it "
            "was applied (schema drift)"
        )


def _reject_newer_database(applied: dict[int, str], migrations: list[tuple[int, str, str]]) -> None:
    if not applied:
        return
    highest_packaged = max((version for version, _, _ in migrations), default=0)
    if max(applied) > highest_packaged:
        # highest_applied is int(stored schema_migrations.version), i.e. row content,
        # so it must not appear in the message. highest_packaged is derived from the
        # packaged migration filenames and is safe to name.
        raise LedgerError(
            "ledger database was written by a newer build than this one supports "
            f"(this build's newest migration is {highest_packaged}); refusing to run"
        )


def _load_migrations() -> list[tuple[int, str, str]]:
    migrations: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for entry in files(_MIGRATIONS_PACKAGE).iterdir():
        match = _MIGRATION_NAME_RE.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in seen:
            raise LedgerError(f"duplicate ledger migration number {version}")
        seen.add(version)
        data = entry.read_bytes()
        migrations.append((version, data.decode("utf-8"), hashlib.sha256(data).hexdigest()))
    migrations.sort(key=lambda item: item[0])
    return migrations


def _apply_migration(conn: sqlite3.Connection, version: int, sql: str, checksum: str) -> None:
    applied_at = datetime.now(tz=timezone.utc).isoformat()
    # Split before opening the transaction so a splitter failure cannot leave an
    # open BEGIN for the caller's finally-close to roll back implicitly.
    statements = _statements(sql)
    try:
        conn.execute("BEGIN")
        for statement in statements:
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
    """Split a migration into individually executable statements.

    Supported grammar: a sequence of SQL statements each terminated by a
    semicolon. Splitting uses :func:`sqlite3.complete_statement` (a wrapper over
    C ``sqlite3_complete``) instead of a naive ``str.split(";")``, so a semicolon
    inside a string literal, an inline ``--`` comment, or a ``CREATE TRIGGER ...
    BEGIN ... END;`` body does not falsely end a statement. This is what lets the
    append-only-enforcement triggers deferred to M1-602/M1-603 be applied by this
    same runner.

    The scan emits at every top-level statement terminator, so more than one
    statement may share a physical line (``CREATE TABLE a(x); CREATE TABLE b(y);``)
    -- ``conn.execute`` rejects a chunk holding two statements, so the split must be
    exact, not line-based. A non-comment remainder after the last complete statement
    means the final statement is unterminated and is rejected.
    """
    statements: list[str] = []
    buffer = ""
    for char in sql:
        buffer += char
        # complete_statement only ever flips to True on a terminating ``;``, so gate
        # the (cheap but non-trivial) call on that character. It stays False while the
        # ``;`` sits inside a string literal, a comment, or an unfinished trigger body.
        if char == ";" and sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                statements.append(statement)
    if _has_executable_sql(buffer):
        raise LedgerError("ledger migration ends with an unterminated statement")
    return statements


def _has_executable_sql(text: str) -> bool:
    """True if ``text`` holds anything beyond blank lines and ``--`` comments."""
    return any(
        stripped and not stripped.startswith("--")
        for stripped in (line.strip() for line in text.splitlines())
    )
