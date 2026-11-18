"""The experiment store: append-only arm forecasts in their own SQLite file (M5-806, AC1/AC2/AC7).

D62 keeps replay arms off the live ledger: a separate file with its own schema and version, so no
live migration is needed, the activation is not retired, and bulk experiment writes never sit
beside the posting path. Rows bind to their live control by ``record_id`` and ``forecast_sha256``
as text, and because there is no cross-file foreign key the writer re-reads the control in the
live ledger -- through the caller's read-only connection -- every time it appends.

**The store is never mistaken for a ledger.** Opening refuses a file that holds
``schema_migrations`` or ``forecast_records``, and the schema holds no table the approval,
submission or posting code reads. Its ``tournament_events`` is the priced client's spend journal,
restricted by trigger to spend and model-call kinds (owner decision on AC7, 2026-11-17).

**One run at a time.** A run holds an exclusive lock on ``<store>.lock`` for its whole duration.
The priced client's replay-cache check sits outside its reservation transaction, so two
concurrent runs of one arm could otherwise both buy the same sample.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Final, Literal, get_args

from whiskeyjack_bot.experiment.errors import ExperimentError
from whiskeyjack_bot.forecast.record import (
    ForecastRecord,
    ForecastRecordError,
    canonical_record_json,
    record_from_json,
    record_sha256,
)
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.ledger import LedgerError, connect
from whiskeyjack_bot.lifecycle import LifecycleError, transaction

EXPERIMENT_STORE_SCHEMA_VERSION: Final = 1

_SCHEMA_RESOURCE: Final = "store_schema_001.sql"

# Tables whose presence means the file is a ledger, not an experiment store.
_LEDGER_TABLES: Final = frozenset({"schema_migrations", "forecast_records"})

SampleOutcome = Literal["forecast", "generation_failed", "outcome_unknown"]

_SAMPLE_COLUMNS: Final[tuple[str, ...]] = (
    "sample_id",
    "arm_sha256",
    "run_id",
    "control_record_id",
    "control_forecast_sha256",
    "sample_index",
    "attempt_id",
    "outcome",
    "failure_code",
    "kept_packet_sha256",
    "request_sha256",
    "record_json",
    "forecast_sha256",
    "raw_output_path",
    "cost_microusd",
    "held_microusd",
    "billed_calls",
    "model_invocations",
    "reported_models_json",
    "reasoning_effort_sent",
    "created_at_utc",
)


@dataclass(frozen=True)
class ArmSample:
    """One arm forecast, as written and as read back (one type for both directions)."""

    sample_id: str
    arm_sha256: str
    run_id: str
    control_record_id: str
    control_forecast_sha256: str
    sample_index: int
    attempt_id: str
    outcome: SampleOutcome
    failure_code: str | None
    kept_packet_sha256: str
    request_sha256: str
    record: ForecastRecord | None
    raw_output_path: str | None
    cost_microusd: int | None
    held_microusd: int
    billed_calls: int
    model_invocations: int | None
    reported_models: tuple[str, ...]
    reasoning_effort_sent: str | None
    created_at_utc: datetime

    @property
    def forecast_sha256(self) -> str | None:
        return None if self.record is None else record_sha256(self.record)


def _schema_sql() -> bytes:
    return files("whiskeyjack_bot.experiment").joinpath(_SCHEMA_RESOURCE).read_bytes()


def _statements(script: str) -> list[str]:
    """Split a schema script into whole statements, trigger bodies kept intact."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip() and not all(
        part.strip().startswith("--") or not part.strip() for part in buffer.splitlines()
    ):
        raise ExperimentError("the experiment store schema has an incomplete statement")
    return statements


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def _refuse_a_ledger(path: Path) -> None:
    """Look at an existing file read-only first, so a ledger is never opened read-write here.

    ``ledger.connect`` sets WAL and other pragmas, which are writes; a mistyped ``--store`` that
    names a ledger must be refused before that, not after.
    """
    try:
        exists = path.is_file()
    except OSError:
        raise ExperimentError(f"cannot read experiment store path {path}") from None
    if not exists:
        return
    try:
        probe = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, OSError, RuntimeError, ValueError):
        raise ExperimentError(f"cannot open experiment store {path}") from None
    try:
        present = _tables(probe)
    except sqlite3.Error:
        raise ExperimentError(f"{path} is not a readable SQLite database") from None
    finally:
        probe.close()
    if present & _LEDGER_TABLES:
        raise ExperimentError(f"{path} is a ledger, not an experiment store; refusing it")


def open_store(path: Path) -> sqlite3.Connection:
    """Open (creating if new) the experiment store at ``path``, verifying its schema.

    Refuses a file that is a ledger, a store of another schema version, and a store whose
    recorded schema checksum differs from this build's -- the last is the store's equivalent of
    the ledger's checksum drift, and a store written under other DDL is not this one.
    """
    _refuse_a_ledger(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
    except (OSError, LedgerError):
        raise ExperimentError(f"cannot open experiment store {path}") from None
    try:
        script = _schema_sql()
        checksum = hashlib.sha256(script).hexdigest()
        present = _tables(conn)
        if present & _LEDGER_TABLES:
            raise ExperimentError(f"{path} is a ledger, not an experiment store; refusing it")
        if not present:
            with transaction(conn):
                for statement in _statements(script.decode("utf-8")):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO experiment_schema (version, applied_at_utc, checksum) "
                    "VALUES (?, ?, ?)",
                    (EXPERIMENT_STORE_SCHEMA_VERSION, _now(), checksum),
                )
        rows = conn.execute("SELECT version, checksum FROM experiment_schema").fetchall()
        if [(row[0], row[1]) for row in rows] != [(EXPERIMENT_STORE_SCHEMA_VERSION, checksum)]:
            raise ExperimentError(
                f"experiment store {path} was written under another schema; refusing it"
            )
    except ExperimentError:
        conn.close()
        raise
    except (sqlite3.Error, LifecycleError, OSError, UnicodeDecodeError):
        conn.close()
        raise ExperimentError(f"cannot initialize or verify experiment store {path}") from None
    return conn


@contextmanager
def store_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on ``<store>.lock`` for one run."""
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        raise ExperimentError(f"cannot open experiment store lock {lock_path}") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ExperimentError(
                f"another run holds the experiment store lock {lock_path}; refusing to start"
            ) from None
        yield
    finally:
        os.close(descriptor)


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        raise ExperimentError("cannot render an experiment store value (value withheld)") from None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def bind_arm(
    conn: sqlite3.Connection,
    *,
    arm_sha256: str,
    spec_json: str,
    settings_sha256: str,
    selection: Sequence[str],
) -> tuple[str, ...]:
    """Record the arm on its first run, or check a later run against it; returns its selection.

    The first run freezes two things beside the spec: the generation settings the arm ran under
    (``settings_sha256``), and the ordered list of control records it covers. A later run under
    other settings is refused -- its rows would not be one arm's -- and resumes over the frozen
    list, so records resolved after the arm started never enter it (SOURCE-ALPHA: each arm runs
    once over the population as it stands when it starts).
    """
    try:
        row = conn.execute(
            "SELECT spec_json, settings_sha256, selection_json, selection_sha256 FROM arms "
            "WHERE arm_sha256 = ?",
            (arm_sha256,),
        ).fetchone()
        if row is None:
            selection_json = canonical_json(list(selection))
            with transaction(conn):
                conn.execute(
                    "INSERT INTO arms (arm_sha256, spec_json, settings_sha256, selection_json, "
                    "selection_sha256, created_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        arm_sha256,
                        spec_json,
                        settings_sha256,
                        selection_json,
                        sha256_text(selection_json),
                        _now(),
                    ),
                )
            return tuple(selection)
    except (sqlite3.Error, LifecycleError):
        raise ExperimentError("cannot record the arm in the experiment store") from None
    stored_spec, stored_settings, stored_selection, stored_digest = (
        row[0],
        row[1],
        row[2],
        row[3],
    )
    if stored_spec != spec_json:
        raise ExperimentError("the stored arm's spec does not match this arm's hash")
    if stored_settings != settings_sha256:
        raise ExperimentError(
            "this run's generation settings differ from the arm's first run; refusing to mix them"
        )
    if type(stored_selection) is not str or sha256_text(stored_selection) != stored_digest:
        raise ExperimentError("the stored arm's frozen selection does not match its digest")
    try:
        frozen = json.loads(stored_selection)
    except ValueError:
        raise ExperimentError("the stored arm's frozen selection is not valid JSON") from None
    if type(frozen) is not list or any(type(item) is not str for item in frozen):
        raise ExperimentError("the stored arm's frozen selection is not a list of record ids")
    return tuple(frozen)


def begin_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    arm_sha256: str,
    live_ledger_path: Path,
    live_schema_version: int,
    ceiling_microusd: int,
) -> None:
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO runs (run_id, arm_sha256, live_ledger_path, live_schema_version, "
                "ceiling_microusd, started_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    arm_sha256,
                    str(live_ledger_path),
                    live_schema_version,
                    ceiling_microusd,
                    _now(),
                ),
            )
    except (sqlite3.Error, LifecycleError):
        raise ExperimentError("cannot record the run in the experiment store") from None


def record_fidelity(
    conn: sqlite3.Connection,
    run_id: str,
    outcomes: Sequence[tuple[str, str | None, str | None]],
) -> None:
    """Persist each judged record's ``(record_id, reason_code, control_effort)`` for the run."""
    try:
        with transaction(conn):
            conn.executemany(
                "INSERT INTO fidelity_outcomes (run_id, control_record_id, reason_code, "
                "control_effort) VALUES (?, ?, ?, ?)",
                [(run_id, record_id, reason, effort) for record_id, reason, effort in outcomes],
            )
    except (sqlite3.Error, LifecycleError):
        raise ExperimentError("cannot record the fidelity outcomes") from None


def finish_run(
    conn: sqlite3.Connection, run_id: str, *, stop_reason: str, counts: Mapping[str, object]
) -> None:
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO run_reports (run_id, stop_reason, counts_json, finished_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (run_id, stop_reason, canonical_json(dict(counts)), _now()),
            )
    except (sqlite3.Error, LifecycleError):
        raise ExperimentError("cannot record the run report") from None


def completed_samples(conn: sqlite3.Connection, arm_sha256: str) -> set[tuple[str, int]]:
    """Every (control record, sample index) this arm already has a row for."""
    try:
        rows = conn.execute(
            "SELECT control_record_id, sample_index FROM arm_samples WHERE arm_sha256 = ?",
            (arm_sha256,),
        ).fetchall()
    except sqlite3.Error:
        raise ExperimentError("cannot read the experiment store") from None
    return {(str(row[0]), int(row[1])) for row in rows}


def append_arm_sample(
    conn: sqlite3.Connection, live: sqlite3.Connection, sample: ArmSample
) -> None:
    """Append one arm forecast after re-reading its control in the live ledger.

    The control must still read back through the verified reader with the ``forecast_sha256``
    the row names: the binding is text, so this is the check that makes it a binding.
    """
    try:
        control = read_forecast_record(live, sample.control_record_id)
    except (ForecastRecordError, sqlite3.Error):
        raise ExperimentError("the arm sample's control record cannot be read") from None
    if record_sha256(control) != sample.control_forecast_sha256:
        raise ExperimentError("the arm sample's control forecast_sha256 does not match the ledger")
    if (sample.outcome == "forecast") != (sample.record is not None):
        raise ExperimentError("an arm sample carries a record exactly when it is a forecast")
    record_json = None if sample.record is None else canonical_record_json(sample.record)
    values = (
        sample.sample_id,
        sample.arm_sha256,
        sample.run_id,
        sample.control_record_id,
        sample.control_forecast_sha256,
        sample.sample_index,
        sample.attempt_id,
        sample.outcome,
        sample.failure_code,
        sample.kept_packet_sha256,
        sample.request_sha256,
        record_json,
        sample.forecast_sha256,
        sample.raw_output_path,
        sample.cost_microusd,
        sample.held_microusd,
        sample.billed_calls,
        sample.model_invocations,
        canonical_json(list(sample.reported_models)),
        sample.reasoning_effort_sent,
        sample.created_at_utc.isoformat(),
    )
    try:
        with transaction(conn):
            conn.execute(
                f"INSERT INTO arm_samples ({', '.join(_SAMPLE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _SAMPLE_COLUMNS)})",
                values,
            )
    except (sqlite3.Error, LifecycleError):
        raise ExperimentError("cannot append the arm sample") from None


def _text(value: object) -> str:
    if type(value) is not str:
        raise ExperimentError("a stored arm sample field is not text")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ExperimentError("a stored arm sample count is not a non-negative integer")
    return value


def _optional_count(value: object) -> int | None:
    return None if value is None else _count(value)


def _sha(value: object) -> str:
    text = _text(value)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ExperimentError("a stored arm sample hash is not a sha256")
    return text


def _sample(row: Sequence[object]) -> ArmSample:
    """One stored row, read back strictly: every field typed, every hash recomputed."""
    if len(row) != len(_SAMPLE_COLUMNS):
        raise ExperimentError("a stored arm sample has the wrong number of fields")
    data = dict(zip(_SAMPLE_COLUMNS, row, strict=True))
    outcome = data["outcome"]
    if type(outcome) is not str or outcome not in get_args(SampleOutcome):
        raise ExperimentError("a stored arm sample's outcome is not a known value")
    record_json = data["record_json"]
    stored_hash = data["forecast_sha256"]
    record: ForecastRecord | None = None
    if (record_json is None) != (stored_hash is None) or (record_json is not None) != (
        outcome == "forecast"
    ):
        raise ExperimentError("a stored arm sample's record and outcome disagree")
    if record_json is not None:
        try:
            record = record_from_json(_text(record_json))
        except ForecastRecordError:
            raise ExperimentError("a stored arm sample's record does not read back") from None
        if record_sha256(record) != _sha(stored_hash):
            raise ExperimentError("a stored arm sample's record does not match its hash")
    models_json = _text(data["reported_models_json"])
    try:
        models = json.loads(models_json)
    except ValueError:
        raise ExperimentError("a stored arm sample's reported models are not JSON") from None
    if type(models) is not list or any(type(item) is not str for item in models):
        raise ExperimentError("a stored arm sample's reported models are not a list of text")
    try:
        created = datetime.fromisoformat(_text(data["created_at_utc"]))
    except ValueError:
        raise ExperimentError("a stored arm sample's timestamp is not ISO-8601") from None
    if created.utcoffset() is None:
        raise ExperimentError("a stored arm sample's timestamp has no offset")
    sample = ArmSample(
        sample_id=_text(data["sample_id"]),
        arm_sha256=_sha(data["arm_sha256"]),
        run_id=_text(data["run_id"]),
        control_record_id=_text(data["control_record_id"]),
        control_forecast_sha256=_sha(data["control_forecast_sha256"]),
        sample_index=_count(data["sample_index"]),
        attempt_id=_text(data["attempt_id"]),
        outcome=outcome,  # type: ignore[arg-type]
        failure_code=_optional_text(data["failure_code"]),
        kept_packet_sha256=_sha(data["kept_packet_sha256"]),
        request_sha256=_sha(data["request_sha256"]),
        record=record,
        raw_output_path=_optional_text(data["raw_output_path"]),
        cost_microusd=_optional_count(data["cost_microusd"]),
        held_microusd=_count(data["held_microusd"]),
        billed_calls=_count(data["billed_calls"]),
        model_invocations=_optional_count(data["model_invocations"]),
        reported_models=tuple(models),
        reasoning_effort_sent=_optional_text(data["reasoning_effort_sent"]),
        created_at_utc=created,
    )
    if canonical_json(list(sample.reported_models)) != models_json:
        raise ExperimentError("a stored arm sample's reported models are not in canonical form")
    if sample.created_at_utc.isoformat() != data["created_at_utc"]:
        raise ExperimentError("a stored arm sample's timestamp is not in canonical form")
    return sample


def read_arm_samples(conn: sqlite3.Connection, arm_sha256: str) -> tuple[ArmSample, ...]:
    """Every stored sample of one arm, verified, in (control record, sample index) order."""
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_SAMPLE_COLUMNS)} FROM arm_samples WHERE arm_sha256 = ? "
            "ORDER BY control_record_id, sample_index",
            (arm_sha256,),
        ).fetchall()
    except sqlite3.Error:
        raise ExperimentError("cannot read the experiment store") from None
    return tuple(_sample(tuple(row)) for row in rows)
