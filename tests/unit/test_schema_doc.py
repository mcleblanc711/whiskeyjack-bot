"""D-1002: `docs/SCHEMA.md` is a total partition of what the ledger and the code have.

The acceptance criterion is *"Every table/field and export version has an auditable
definition"*. A document checked row by row can only say that the rows it has are right; it
cannot notice the row it lacks, which is how D-1001's exit table stayed wrong for two review
rounds (T-908). So each test here builds the **universe** from the code or from a freshly
initialized ledger -- never from a list written in this file -- parses the document's table,
and asserts the two are equal as sets: nothing undocumented, nothing documented that does not
exist.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, get_args

import pytest
from report_rows import build_ledger

import whiskeyjack_bot.ledger as ledger_module
from whiskeyjack_bot.export import export_ledger
from whiskeyjack_bot.ledger import initialize_ledger
from whiskeyjack_bot.lifecycle import _LEGAL_TRANSITIONS, LifecycleEventType, LifecycleStatus
from whiskeyjack_bot.platform_scores import COMPARISON_BASELINES, PLATFORM_METRICS
from whiskeyjack_bot.platform_scores import IMPLEMENTATION_VERSIONS as PLATFORM_VERSIONS
from whiskeyjack_bot.report import (
    AXES,
    MANIFEST_FILENAME,
    RECORDS_FILENAME,
    REPORT_FILENAME,
    STATES,
    WARNING_CODES,
    write_report,
)
from whiskeyjack_bot.resolution import DEFINITIVE_KINDS, RESOLUTION_KINDS, SCORABLE_KINDS
from whiskeyjack_bot.scoring import BINARY_METRICS, LOCAL_METRICS, MULTICLASS_METRICS
from whiskeyjack_bot.scoring import IMPLEMENTATION_VERSIONS as LOCAL_VERSIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "SCHEMA.md"
SRC = REPO_ROOT / "src" / "whiskeyjack_bot"

_HEADING = re.compile(r"^(#{1,4}) (.+?)\s*$")


# ── parsing the document ─────────────────────────────────────────────────────


def _sections() -> list[tuple[int, str, str]]:
    """``(level, heading, body)`` for every heading, in order."""
    sections: list[tuple[int, str, list[str]]] = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append((len(match.group(1)), match.group(2), []))
        elif sections:
            sections[-1][2].append(line)
    return [(level, heading, "\n".join(body)) for level, heading, body in sections]


def _section(heading: str) -> str:
    (body,) = [body for _, name, body in _sections() if name == heading]
    return body


def _children(parent: str) -> list[tuple[str, str]]:
    """The level-3 sections under the level-2 heading ``parent``."""
    sections = _sections()
    start = next(i for i, (level, name, _) in enumerate(sections) if level == 2 and name == parent)
    children: list[tuple[str, str]] = []
    for level, name, body in sections[start + 1 :]:
        if level <= 2:
            break
        if level == 3:
            children.append((name, body))
    return children


def _table(body: str) -> list[list[str]]:
    """The first markdown table in ``body``, header and separator dropped, cells stripped."""
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    rows: list[list[str]] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _code(cell: str) -> str:
    assert cell.startswith("`") and cell.endswith("`"), cell
    return cell[1:-1]


def _field_rows(body: str) -> set[str]:
    rows = _table(body)
    assert all(len(row) == 2 and row[1] for row in rows), "every field needs a definition"
    fields = [_code(row[0]) for row in rows]
    assert len(fields) == len(set(fields)), "a field is defined twice"
    return set(fields)


# ── the ledger's own shape ───────────────────────────────────────────────────


def _schema(path: Path) -> dict[str, dict[str, str]]:
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: {row[1]: row[2] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            for table in tables
        }
    finally:
        conn.close()


@pytest.fixture(scope="module")
def fresh_ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("schema-doc") / "ledger.sqlite3"
    initialize_ledger(path)
    return path


def _documented_tables() -> dict[str, tuple[str, str, list[list[str]]]]:
    """table -> (introduced, mutability, column rows)."""
    documented: dict[str, tuple[str, str, list[list[str]]]] = {}
    for heading, body in _children("Ledger tables"):
        table = _code(heading)
        introduced = re.search(r"\*\*Introduced:\*\* `(\d{3})`", body)
        mutability = re.search(r"\*\*Mutability:\*\* ([a-z-]+)\.", body)
        assert introduced and mutability, table
        assert table not in documented, table
        documented[table] = (introduced.group(1), mutability.group(1), _table(body))
    return documented


def test_every_table_and_column_is_documented_and_nothing_else(fresh_ledger: Path) -> None:
    actual = {
        (table, column, declared)
        for table, columns in _schema(fresh_ledger).items()
        for column, declared in columns.items()
    }
    documented = set()
    for table, (_, _, rows) in _documented_tables().items():
        for row in rows:
            column, declared, _, definition = row
            assert definition, (table, column)
            documented.add((table, _code(column), declared))
    assert documented == actual
    assert len(actual) == 170  # the count is a witness that both sides are non-trivial


def test_each_since_is_the_migration_that_introduced_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay the packaged migrations one at a time and record where each column first exists.

    A table rebuilt by a later migration still first exists at its creator, which is the
    meaning of "since" here.
    """
    packaged = ledger_module._load_migrations()
    path = tmp_path / "replay.sqlite3"
    first_table: dict[str, int] = {}
    first_column: dict[tuple[str, str], int] = {}
    for version, _, _ in packaged:
        monkeypatch.setattr(
            ledger_module,
            "_load_migrations",
            lambda version=version: [m for m in packaged if m[0] <= version],
        )
        initialize_ledger(path)
        for table, columns in _schema(path).items():
            first_table.setdefault(table, version)
            for column in columns:
                first_column.setdefault((table, column), version)
    # Witness that the replay really stepped: tables and columns arrive at many versions.
    assert len(first_table) == 15 and len(set(first_column.values())) >= 8

    documented_tables = _documented_tables()
    assert {table: int(since) for table, (since, _, _) in documented_tables.items()} == first_table
    assert {
        (table, _code(row[0])): int(row[2])
        for table, (_, _, rows) in documented_tables.items()
        for row in rows
    } == first_column


def _mutability(path: Path) -> dict[str, str]:
    """Each table's class, read from its triggers' SQL.

    ``append-only``: an unconditional BEFORE UPDATE and an unconditional BEFORE DELETE.
    ``annotatable``: an unconditional BEFORE DELETE and only conditional UPDATE guards.
    ``unguarded``: no UPDATE or DELETE trigger at all. Anything else is its own answer, and
    no document row can match it.
    """
    guards: dict[str, dict[str, set[bool]]] = {}
    conn = sqlite3.connect(path)
    try:
        for table, sql in conn.execute(
            "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'trigger'"
        ):
            text = " ".join(sql.split())
            match = re.search(r"BEFORE (UPDATE|DELETE) ON (\w+)", text)
            if match is None:
                continue
            head, body = text.split(" BEGIN ", 1)
            conditional = " WHEN " in head or " WHERE " in body
            guards.setdefault(table, {}).setdefault(match.group(1), set()).add(conditional)
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
    finally:
        conn.close()
    classes: dict[str, str] = {}
    for table in tables:
        update = guards.get(table, {}).get("UPDATE", set())
        delete = guards.get(table, {}).get("DELETE", set())
        if False in update and False in delete:
            classes[table] = "append-only"
        elif False in delete and update and False not in update:
            classes[table] = "annotatable"
        elif not update and not delete:
            classes[table] = "unguarded"
        else:
            classes[table] = f"unclassified(update={sorted(update)}, delete={sorted(delete)})"
    return classes


def test_each_tables_mutability_class_is_what_its_triggers_enforce(fresh_ledger: Path) -> None:
    documented = {table: mutability for table, (_, mutability, _) in _documented_tables().items()}
    actual = _mutability(fresh_ledger)
    assert documented == actual
    assert set(actual.values()) == {"append-only", "annotatable", "unguarded"}


# ── lifecycle, resolution kinds, metrics ─────────────────────────────────────


def test_the_transition_table_is_the_legal_transitions() -> None:
    rows = _table(_section("Lifecycle"))
    documented = {(_code(event), _code(source), _code(target)) for event, source, target in rows}
    assert len(documented) == len(rows)
    assert documented == set(_LEGAL_TRANSITIONS)
    assert {event for event, _, _ in documented} == set(get_args(LifecycleEventType))
    statuses = {status for _, source, target in documented for status in (source, target)}
    assert statuses == set(get_args(LifecycleStatus))


def test_the_resolution_kind_table_is_the_kind_vocabulary() -> None:
    rows = _table(_section("Resolution kinds"))
    documented = {
        _code(kind): (scorable == "yes", moves == "yes") for kind, scorable, moves, _ in rows
    }
    assert len(documented) == len(rows)
    assert documented == {
        kind: (kind in SCORABLE_KINDS, kind in DEFINITIVE_KINDS) for kind in RESOLUTION_KINDS
    }


def test_the_metric_table_is_every_score_metric_with_its_source() -> None:
    rows = _table(_section("Score metrics"))
    documented = {
        _code(metric): (provenance, types, baseline, _code(version))
        for metric, provenance, types, baseline, version in rows
    }
    assert len(documented) == len(rows)
    expected: dict[str, tuple[str, str, str, str]] = {}
    for metric in LOCAL_METRICS:
        types = "binary" if metric in BINARY_METRICS else "multiple_choice"
        assert metric in BINARY_METRICS or metric in MULTICLASS_METRICS
        expected[metric] = ("local", types, "none", LOCAL_VERSIONS[metric])  # type: ignore[index]
    for metric in PLATFORM_METRICS:
        expected[metric] = (
            "platform",
            "all",
            f"`{COMPARISON_BASELINES[metric]}`",  # type: ignore[index]
            PLATFORM_VERSIONS[metric],  # type: ignore[index]
        )
    assert documented == expected


# ── versions and anchors ─────────────────────────────────────────────────────


def _module_constants(suffix: str) -> set[tuple[str, str, str]]:
    """``(file, name, value)`` for every module-level constant named ``*<suffix>`` in src/."""
    found: set[tuple[str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if isinstance(target, ast.Name) and target.id.endswith(suffix) and value is not None:
                constant = ast.literal_eval(value)
                found.add((str(path.relative_to(SRC)), target.id, str(constant)))
    return found


def test_the_versions_table_is_every_schema_version_constant_with_its_value() -> None:
    rows = _table(_section("Versions"))
    documented = set()
    for _, value, anchor, meaning in rows:
        assert meaning, anchor
        path, name = _code(anchor).split(":")
        documented.add((path, name, _code(value)))
    assert len(documented) == len(rows)
    actual = _module_constants("_SCHEMA_VERSION")
    assert documented == actual
    assert len(actual) >= 13


def _module_symbols(path: Path) -> set[str]:
    symbols: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            symbols.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols.add(node.target.id)
    return symbols


def test_every_code_anchor_in_the_document_resolves() -> None:
    anchors = set(re.findall(r"`([a-z_/]+\.py):([A-Za-z_]\w*)`", DOC.read_text(encoding="utf-8")))
    assert len(anchors) >= 20
    unresolved = [
        f"{path}:{symbol}"
        for path, symbol in sorted(anchors)
        if not (SRC / path).is_file() or symbol not in _module_symbols(SRC / path)
    ]
    assert unresolved == []


# ── the derived artifacts' fields ────────────────────────────────────────────


def _paths(value: Any, prefix: str = "") -> set[str]:
    """Every key path in ``value``; ``[]`` marks an array element.

    Keys of a ``states`` object are the ``RecordState`` members and of an ``excluded`` object
    the ``Exclusion`` members; both are documented once, as ``<state>`` and ``<exclusion>``.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            segment = key
            if prefix.endswith("states"):
                segment = "<state>"
            elif prefix.endswith("excluded"):
                segment = "<exclusion>"
            path = f"{prefix}.{segment}" if prefix else segment
            found.add(path)
            found |= _paths(child, path)
    elif isinstance(value, list):
        for item in value:
            found |= _paths(item, f"{prefix}[]")
    return found


def test_the_export_manifest_fields_are_documented_and_nothing_else(
    fresh_ledger: Path, tmp_path: Path
) -> None:
    observed: set[str] = set()
    for export_format in ("jsonl", "parquet"):
        destination = tmp_path / export_format
        export_ledger(fresh_ledger, destination, export_format=export_format)  # type: ignore[arg-type]
        observed |= _paths(json.loads((destination / "manifest.json").read_text("utf-8")))
    assert "writer.pyarrow" in observed and "tables[].columns" in observed
    assert _field_rows(_section("Export contract (`EXPORT_SCHEMA_VERSION` 1)")) == observed


@pytest.fixture(scope="module")
def report_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("schema-doc-report")
    ledger = build_ledger(root / "ledger.sqlite3")
    write_report(ledger, root / "report")
    return root / "report"


def test_the_records_jsonl_fields_are_documented_and_nothing_else(report_dir: Path) -> None:
    observed: set[str] = set()
    for line in (report_dir / RECORDS_FILENAME).read_text("utf-8").splitlines():
        observed |= _paths(json.loads(line))
    # Non-vacuity: the fixture populates every array and the nullable object.
    assert {"scores[].metric", "source_categories[].slug", "resolution.kind"} <= observed
    assert _field_rows(_section(f"`{RECORDS_FILENAME}`")) == observed


def test_the_report_json_fields_are_documented_and_nothing_else(report_dir: Path) -> None:
    observed = _paths(json.loads((report_dir / REPORT_FILENAME).read_text("utf-8")))
    assert {
        "axes[].groups[].calibration.bins[].mean_forecast",
        "axes[].groups[].scores[].sample_sd",
        "axes[].groups[].key.category_id",
        "warnings[].code",
        "population.excluded.<exclusion>",
    } <= observed
    assert _field_rows(_section(f"`{REPORT_FILENAME}`")) == observed


def test_the_report_manifest_fields_are_documented_and_nothing_else(report_dir: Path) -> None:
    observed = _paths(json.loads((report_dir / MANIFEST_FILENAME).read_text("utf-8")))
    assert _field_rows(_section(f"`{MANIFEST_FILENAME}`")) == observed


def test_the_report_contract_names_every_state_axis_and_warning() -> None:
    contract = _section("Report contract (`REPORT_SCHEMA_VERSION` 1)")
    for name in (*STATES, *AXES):
        assert f"`{name}`" in contract, name
    (warning_row,) = [
        row for row in _table(_section(f"`{REPORT_FILENAME}`")) if row[0] == "`warnings[].code`"
    ]
    assert set(re.findall(r"`(\w+)`", warning_row[1])) == set(WARNING_CODES)
