"""M5-804: the attribution report dataset, derived from a real ledger.

The acceptance criterion is *"Export contains counts, calibration bins and score summaries
with small-sample warnings"*. The fixture ledger (``tests/report_rows.py``) is written through
the production writers and reaches every record state, every axis and both score provenances,
and **every expected number below is hand arithmetic over that fixture**, not a second run of
the code under test (an oracle that restates the code's rule proves nothing).

Replay is tested on a real copy, and only after asserting the fixture actually holds scored
records -- a replay test over an empty report compares nothing to nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from report_rows import EXPECTED_STATES, SPECS, SPOT_PEER, Spec, build_ledger, seed
from resolution_rows import SCORE_DATA

import whiskeyjack_bot.ledger as ledger_module
from whiskeyjack_bot.ledger import LedgerError, connect, initialize_ledger
from whiskeyjack_bot.report import (
    AXES,
    CALIBRATION_BIN_EDGES,
    MANIFEST_FILENAME,
    OVERLAPPING_AXES,
    RECORDS_FILENAME,
    REPORT_FILENAME,
    REPORT_SCHEMA_VERSION,
    SMALL_SAMPLE_THRESHOLD,
    STATES,
    ReportError,
    write_report,
)
from whiskeyjack_bot.tournament_state import append

NOW = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)
CANARY = "sk-report-canary-7781"


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_ledger(tmp_path_factory.mktemp("report-template") / "ledger.sqlite3")


def _copy(source: Path, target: Path) -> Path:
    """A consistent copy through SQLite's own backup API, never a file copy of a WAL ledger."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


@pytest.fixture
def ledger(template: Path, tmp_path: Path) -> Path:
    return _copy(template, tmp_path / "ledger" / "ledger.sqlite3")


@pytest.fixture(scope="module")
def generated(template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp("report-out") / "report"
    write_report(template, destination, now=NOW)
    return destination


def _report(destination: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((destination / REPORT_FILENAME).read_text("utf-8"))
    return loaded


def _records(destination: Path) -> list[dict[str, Any]]:
    text = (destination / RECORDS_FILENAME).read_text("utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _axis(report: dict[str, Any], name: str) -> dict[str, Any]:
    (axis,) = [axis for axis in report["axes"] if axis["axis"] == name]
    return axis


def _group(report: dict[str, Any], name: str, key: dict[str, Any]) -> dict[str, Any]:
    (group,) = [group for group in _axis(report, name)["groups"] if group["key"] == key]
    return group


def _cell(group: dict[str, Any], metric: str) -> dict[str, Any]:
    (cell,) = [cell for cell in group["scores"] if cell["metric"] == metric]
    return cell


# ── states and population ────────────────────────────────────────────────────


def test_every_record_is_in_records_jsonl_in_its_expected_state(generated: Path) -> None:
    rows = _records(generated)
    assert [row["record_id"] for row in rows] == sorted(EXPECTED_STATES)
    assert {row["record_id"]: row["state"] for row in rows} == EXPECTED_STATES
    # Non-vacuity: the fixture reaches every state the vocabulary has.
    assert set(EXPECTED_STATES.values()) == set(STATES)


def test_the_test_tournament_record_is_kept_marked_and_counted_never_summarized(
    generated: Path,
) -> None:
    rows = {row["record_id"]: row for row in _records(generated)}
    assert rows["rec-15"]["included"] is False
    assert rows["rec-15"]["exclusion"] == "test_tournament"
    assert all(row["included"] for key, row in rows.items() if key != "rec-15")
    report = _report(generated)
    assert report["population"]["records"] == 16
    assert report["population"]["excluded"] == {"test_tournament": 1}
    assert report["population"]["included"] == 15
    # rec-15's spot-peer score (90.0) is in no cell anywhere.
    for axis in report["axes"]:
        assert "bot-testing-area" not in json.dumps(axis)
        for group in axis["groups"]:
            for cell in group["scores"]:
                assert cell["max"] != 90.0


def test_the_population_states_partition_the_included_records(generated: Path) -> None:
    report = _report(generated)
    states = report["population"]["states"]
    assert list(states) == sorted(STATES)  # canonical JSON sorts keys; every state present
    assert states == {
        "not_posted": 1,
        "superseded": 1,
        "awaiting_resolution": 1,
        "withheld": 1,
        "unresolved": 1,
        "annulled": 1,
        "ambiguous": 1,
        "resolved_unscored": 1,
        "scored": 7,
    }
    assert sum(states.values()) == report["population"]["included"]


# ── score summaries ──────────────────────────────────────────────────────────


def test_the_platform_spot_peer_cell_is_the_latest_score_of_each_scored_subject(
    generated: Path,
) -> None:
    """Seven subjects: not rec-13 (superseded), rec-15 (excluded) or rec-10/rec-16's stale rows."""
    cell = _cell(_group(_report(generated), "all", {}), "platform_spot_peer_score")
    assert cell["n"] == 7 == len(SPOT_PEER)
    assert cell["sum"] == 5.0  # 10 - 20 + 5 + 1 + 2 + 3 + 4
    assert cell["mean"] == pytest.approx(5.0 / 7.0, rel=1e-15)
    assert (cell["min"], cell["max"]) == (-20.0, 10.0)
    assert cell["provenance"] == "platform"
    assert cell["comparison_baseline"] == "peer"
    assert cell["implementation_version"] == "platform_spot_peer_score/metaculus_score_data/1"
    assert cell["small_sample"] is True
    deviations = [(value - 5.0 / 7.0) ** 2 for value in SPOT_PEER.values()]
    assert cell["sample_sd"] == pytest.approx(math.sqrt(sum(deviations) / 6), rel=1e-12)


def test_a_constant_platform_score_summarizes_to_itself(generated: Path) -> None:
    cell = _cell(_group(_report(generated), "all", {}), "platform_peer_score")
    value = SCORE_DATA["peer_score"]
    assert (cell["n"], cell["min"], cell["max"], cell["mean"]) == (7, value, value, value)
    assert cell["sample_sd"] == 0.0
    assert cell["comparison_baseline"] == "peer"


def test_the_local_binary_brier_cell_is_hand_arithmetic(generated: Path) -> None:
    """(p - o)^2 over the four scored binary subjects, rec-16 against its SECOND outcome (no)."""
    cell = _cell(_group(_report(generated), "all", {}), "local_brier_binary")
    expected = [(0.3 - 1) ** 2, (0.75 - 0) ** 2, (0.6 - 1) ** 2, (0.9 - 0) ** 2]
    assert cell["n"] == 4
    assert cell["mean"] == pytest.approx(0.505625, rel=1e-12)
    assert cell["min"] == pytest.approx(min(expected))
    assert cell["max"] == pytest.approx(max(expected))
    assert cell["provenance"] == "local"
    assert cell["comparison_baseline"] is None


def test_local_and_platform_never_share_a_cell(generated: Path) -> None:
    report = _report(generated)
    seen = set()
    for axis in report["axes"]:
        for group in axis["groups"]:
            keys = [(cell["metric"], cell["implementation_version"]) for cell in group["scores"]]
            assert len(keys) == len(set(keys))
            for cell in group["scores"]:
                prefix = cell["metric"].split("_", 1)[0]
                assert cell["provenance"] == prefix
                assert (cell["comparison_baseline"] is None) == (prefix == "local")
                seen.add(prefix)
    assert seen == {"local", "platform"}


def test_the_multiclass_cell_holds_the_one_multiple_choice_subject(generated: Path) -> None:
    group = _group(_report(generated), "question_type", {"question_type": "multiple_choice"})
    assert _cell(group, "local_brier_multiclass")["n"] == 1
    assert _cell(group, "platform_spot_peer_score")["mean"] == 5.0
    assert not [cell for cell in group["scores"] if cell["metric"].endswith("_binary")]


# ── calibration ──────────────────────────────────────────────────────────────


def test_calibration_bins_are_hand_arithmetic_and_an_edge_opens_its_bin(generated: Path) -> None:
    """rec-01 0.3 yes, rec-02 0.75 no, rec-03 0.05 yes, rec-14 0.6 yes, rec-16 0.9 no.

    Not rec-13 (superseded), rec-15 (excluded), rec-10 (retracted), rec-08/09 (ambiguous,
    withheld) or rec-11 (never observed). 0.3 is an edge and lands in [0.3, 0.4).
    """
    calibration = _group(_report(generated), "all", {})["calibration"]
    assert calibration["n"] == 5
    assert calibration["small_sample"] is True
    bins = calibration["bins"]
    assert [(b["lower"], b["upper"]) for b in bins] == list(
        zip(CALIBRATION_BIN_EDGES[:-1], CALIBRATION_BIN_EDGES[1:], strict=True)
    )
    occupied = {
        b["lower"]: (b["n"], b["mean_forecast"], b["observed_frequency"]) for b in bins if b["n"]
    }
    assert occupied == {
        0.0: (1, 0.05, 1.0),
        0.3: (1, 0.3, 1.0),
        0.6: (1, 0.6, 1.0),
        0.7: (1, 0.75, 0.0),
        0.9: (1, 0.9, 0.0),
    }
    empty = [b for b in bins if not b["n"]]
    assert all(b["mean_forecast"] is None and b["observed_frequency"] is None for b in empty)
    assert all(b["small_sample"] for b in bins)


def test_calibration_is_binary_only(generated: Path) -> None:
    report = _report(generated)
    assert report["parameters"]["calibration_question_types"] == ["binary"]
    for qtype in ("multiple_choice", "numeric", "discrete"):
        group = _group(report, "question_type", {"question_type": qtype})
        assert group["calibration"]["n"] == 0


# ── axes ─────────────────────────────────────────────────────────────────────


def _counts(report: dict[str, Any], name: str) -> dict[str, int]:
    return {
        json.dumps(group["key"], sort_keys=True): group["records"]
        for group in _axis(report, name)["groups"]
    }


def test_every_axis_groups_the_included_records_as_hand_counted(generated: Path) -> None:
    report = _report(generated)
    assert [axis["axis"] for axis in report["axes"]] == list(AXES)
    assert _counts(report, "all") == {"{}": 15}
    assert _counts(report, "tournament_id") == {'{"tournament_id": "33125"}': 15}
    assert _counts(report, "question_type") == {
        '{"question_type": "binary"}': 11,
        '{"question_type": "discrete"}': 1,
        '{"question_type": "multiple_choice"}': 1,
        '{"question_type": "numeric"}': 2,
    }
    assert _counts(report, "model") == {
        '{"model_name": "openrouter/other-model", "model_provider": "openrouter"}': 2,
        '{"model_name": "openrouter/test-model", "model_provider": "openrouter"}': 13,
    }
    assert _counts(report, "prompt") == {
        f'{{"prompt_sha256": "{"b" * 64}", "prompt_version": "1.1.0"}}': 14,
        f'{{"prompt_sha256": "{"e" * 64}", "prompt_version": "1.2.0"}}': 1,
    }
    assert _counts(report, "question_domain") == {
        '{"question_domain": "econ_data"}': 1,
        '{"question_domain": null}': 14,
    }
    assert _counts(report, "source_category") == {
        '{"category_id": 10}': 3,
        '{"category_id": 20}': 2,
        '{"category_id": null}': 11,
    }
    assert _counts(report, "reasoning_strategy_tag") == {
        '{"tag": "base_rate"}': 14,
        '{"tag": "trend"}': 1,
        '{"tag": null}': 1,
    }
    assert _counts(report, "evidence_gap") == {
        '{"code": "evidence_poor"}': 2,
        '{"code": "named_source_absent"}': 2,
        '{"code": null}': 12,
    }


def test_a_category_is_keyed_on_its_id_and_labelled_by_every_slug_seen(generated: Path) -> None:
    report = _report(generated)
    assert _group(report, "source_category", {"category_id": 10})["labels"] == [
        "economy",
        "economy-renamed",
    ]
    assert _group(report, "source_category", {"category_id": None})["labels"] == []


def test_overlapping_axes_are_flagged_and_nothing_totals_their_groups(generated: Path) -> None:
    report = _report(generated)
    for axis in report["axes"]:
        assert set(axis) == {"axis", "overlapping", "groups"}  # no total field on any axis
        assert axis["overlapping"] is (axis["axis"] in OVERLAPPING_AXES)
        total = sum(group["records"] for group in axis["groups"])
        if axis["overlapping"]:
            # Non-vacuity: every overlapping axis really does double-count here.
            assert total > report["population"]["included"]
        else:
            assert total == report["population"]["included"]
    assert {"code": "overlapping_axes", "count": 3} == {
        key: value
        for warning in report["warnings"]
        if warning["code"] == "overlapping_axes"
        for key, value in warning.items()
        if key != "message"
    }


def test_group_states_partition_each_group(generated: Path) -> None:
    for axis in _report(generated)["axes"]:
        for group in axis["groups"]:
            assert sum(group["states"].values()) == group["records"]
            assert set(group["states"]) == set(STATES)


def test_model_cost_counts_unknown_as_unknown_never_as_free(generated: Path) -> None:
    report = _report(generated)
    assert _group(report, "all", {})["model_cost"] == {
        "known": 2,
        "unknown": 13,
        "known_sum_usd": 0.75,
    }
    model = _group(
        report,
        "model",
        {"model_provider": "openrouter", "model_name": "openrouter/other-model"},
    )
    assert model["model_cost"] == {"known": 0, "unknown": 2, "known_sum_usd": None}


def test_warnings_carry_counts_only(generated: Path) -> None:
    warnings = {w["code"]: w["count"] for w in _report(generated)["warnings"]}
    assert warnings["unknown_model_cost"] == 13
    assert warnings["resolved_unscored"] == 1
    assert warnings["stale_score_rows"] == 12  # rec-10 and rec-16, six rows each
    assert warnings["small_sample"] > 0
    assert SMALL_SAMPLE_THRESHOLD == 30


# ── inputs, binding and replay ───────────────────────────────────────────────


def test_each_record_row_cites_the_ledger_rows_it_was_computed_from(
    generated: Path, template: Path
) -> None:
    conn = sqlite3.connect(f"{template.resolve().as_uri()}?mode=ro", uri=True)
    try:
        for row in _records(generated):
            (digest,) = conn.execute(
                "SELECT forecast_sha256 FROM forecast_records WHERE record_id = ?",
                (row["record_id"],),
            ).fetchone()
            assert row["forecast_sha256"] == digest
            latest = conn.execute(
                "SELECT event_id, observation_sha256, source_response_sha256 FROM "
                "resolution_events WHERE forecast_record_id = ? ORDER BY event_id DESC LIMIT 1",
                (row["record_id"],),
            ).fetchone()
            if latest is None:
                assert row["resolution"] is None
                continue
            resolution = row["resolution"]
            assert (
                resolution["event_id"],
                resolution["observation_sha256"],
                resolution["source_response_sha256"],
            ) == latest
            for score in row["scores"]:
                stored = conn.execute(
                    "SELECT metric, value, resolution_event_id FROM score_events "
                    "WHERE event_id = ?",
                    (score["event_id"],),
                ).fetchone()
                assert stored == (score["metric"], score["value"], latest[0])
    finally:
        conn.close()


def test_the_report_binds_itself_to_the_records_file_and_the_manifest_to_both(
    generated: Path,
) -> None:
    records = (generated / RECORDS_FILENAME).read_bytes()
    report = (generated / REPORT_FILENAME).read_bytes()
    manifest = json.loads((generated / MANIFEST_FILENAME).read_text("utf-8"))
    assert _report(generated)["records_sha256"] == hashlib.sha256(records).hexdigest()
    assert manifest["report_schema_version"] == REPORT_SCHEMA_VERSION == 1
    assert manifest["ledger_schema_version"] == ledger_module.LEDGER_SCHEMA_VERSION
    assert manifest["generated_at_utc"] == "2026-09-24T05:00:00Z"
    assert manifest["files"] == [
        {"name": RECORDS_FILENAME, "rows": 16, "sha256": hashlib.sha256(records).hexdigest()},
        {"name": REPORT_FILENAME, "rows": 1, "sha256": hashlib.sha256(report).hexdigest()},
    ]


def test_a_report_on_a_backup_copy_is_byte_identical(template: Path, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_report(template, first, now=NOW)
    write_report(_copy(template, tmp_path / "copy" / "ledger.sqlite3"), second, now=NOW)
    # Non-vacuity: this compares scored subjects, not two empty reports.
    assert _report(first)["population"]["states"]["scored"] == 7
    for name in (RECORDS_FILENAME, REPORT_FILENAME, MANIFEST_FILENAME):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def _fingerprint(db: Path) -> dict[str, str | None]:
    """The database and its WAL. ``-shm`` is lock state every reader writes (see export)."""
    return {
        suffix: hashlib.sha256(Path(f"{db}{suffix}").read_bytes()).hexdigest()
        if Path(f"{db}{suffix}").exists()
        else None
        for suffix in ("", "-wal")
    }


def test_reporting_never_changes_the_ledger(ledger: Path, tmp_path: Path) -> None:
    """The database and a WAL holding uncheckpointed frames are byte-identical afterwards.

    The writer stays open across the report, the state a live worker leaves the ledger in:
    closing it would checkpoint the frames away and the WAL half would compare nothing.
    """
    writer = connect(ledger)
    try:
        (digest,) = writer.execute(
            "SELECT forecast_sha256 FROM forecast_records WHERE record_id = 'rec-03'"
        ).fetchone()
        append(
            writer, "evidence_gap", "rec-03", {"code": "evidence_poor", "forecast_sha256": digest}
        )
        before = _fingerprint(ledger)
        assert before["-wal"] is not None, "the fixture must leave uncheckpointed frames"
        write_report(ledger, tmp_path / "out", now=NOW)
        assert _fingerprint(ledger) == before
    finally:
        writer.close()
    # And the frames were read, not skipped (immutable=1's silent failure): the marker that
    # exists only in the WAL reached the report.
    (row,) = [row for row in _records(tmp_path / "out") if row["record_id"] == "rec-03"]
    assert row["evidence_gaps"] == ["evidence_poor"]


def test_the_report_reads_one_snapshot_even_while_a_writer_commits(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker committed after the first record is read is invisible to every later read.

    Without the one deferred transaction each read would take its own snapshot, and a run
    committing mid-report could give one record facts from after the others'. The writer
    commits into rec-16 -- read last -- right after rec-01 is read; deleting the report's
    ``BEGIN`` must turn this red.
    """
    from whiskeyjack_bot import report as report_module

    writer = connect(ledger)
    (digest,) = writer.execute(
        "SELECT forecast_sha256 FROM forecast_records WHERE record_id = 'rec-16'"
    ).fetchone()
    original = report_module.read_facts
    calls: list[str] = []

    def read_then_commit(connection: sqlite3.Connection, record_id: str) -> Any:
        facts = original(connection, record_id)
        calls.append(record_id)
        if record_id == "rec-01":
            append(
                writer,
                "evidence_gap",
                "rec-16",
                {"code": "evidence_poor", "forecast_sha256": digest},
            )
        return facts

    monkeypatch.setattr(report_module, "read_facts", read_then_commit)
    try:
        write_report(ledger, tmp_path / "out", now=NOW)
    finally:
        writer.close()
        monkeypatch.undo()
    assert calls[0] == "rec-01" and calls[-1] == "rec-16"
    (row,) = [row for row in _records(tmp_path / "out") if row["record_id"] == "rec-16"]
    assert row["evidence_gaps"] == []
    # The commit really happened: a second report sees it.
    write_report(ledger, tmp_path / "again", now=NOW)
    (row,) = [row for row in _records(tmp_path / "again") if row["record_id"] == "rec-16"]
    assert row["evidence_gaps"] == ["evidence_poor"]


def test_an_empty_ledger_reports_zero_everywhere(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    initialize_ledger(path)
    result = write_report(path, tmp_path / "out", now=NOW)
    assert (result.records, result.included) == (0, 0)
    report = _report(tmp_path / "out")
    assert all(axis["groups"] == [] for axis in report["axes"])
    assert [w["code"] for w in report["warnings"]] == ["overlapping_axes"]
    assert (tmp_path / "out" / RECORDS_FILENAME).read_bytes() == b""


# ── refusals ─────────────────────────────────────────────────────────────────


def _drop_triggers(path: Path, *names: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    for name in names:
        conn.execute(f"DROP TRIGGER {name}")
    return conn


def test_a_tampered_platform_score_refuses_the_report_without_echoing_it(
    ledger: Path, tmp_path: Path
) -> None:
    conn = _drop_triggers(ledger, "score_events_block_update")
    conn.execute(
        "UPDATE score_events SET value = 424242.4242 WHERE metric = 'platform_spot_peer_score' "
        "AND forecast_record_id = 'rec-05'"
    )
    conn.close()
    with pytest.raises(ReportError, match="platform score") as caught:
        write_report(ledger, tmp_path / "out", now=NOW)
    assert "424242" not in str(caught.value)
    assert not (tmp_path / "out").exists()


def test_a_tampered_record_column_refuses_the_report(ledger: Path, tmp_path: Path) -> None:
    conn = _drop_triggers(ledger, "forecast_records_block_update")
    conn.execute("UPDATE forecast_records SET model_name = ? WHERE record_id = 'rec-01'", (CANARY,))
    conn.close()
    with pytest.raises(ReportError, match="forecast record") as caught:
        write_report(ledger, tmp_path / "out", now=NOW)
    assert CANARY not in str(caught.value)


def _plant_marker(ledger: Path, record_id: str, data: str) -> None:
    conn = sqlite3.connect(ledger)
    conn.execute(
        "INSERT INTO tournament_events (event_id, kind, scope, data, created_at_utc) "
        "VALUES ('planted', 'evidence_gap', ?, ?, '2026-09-10T00:00:00+00:00')",
        (record_id, data),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("data", "rule"),
    [
        (json.dumps({"code": "evidence_poor", "forecast_sha256": "f" * 64}), "forecast hash"),
        (json.dumps({"code": CANARY, "forecast_sha256": "f" * 64}), "unrecognized code"),
        (json.dumps({"code": [CANARY], "forecast_sha256": "f" * 64}), "unrecognized code"),
        (json.dumps({"code": {"k": CANARY}}), "unrecognized code"),
        (json.dumps({"forecast_sha256": CANARY}), "unrecognized code"),
        (json.dumps([CANARY]), "not an object"),
        (json.dumps(CANARY), "not an object"),
        # Valid JSON to SQLite's `json_valid` CHECK (012) and past Python's recursion limit.
        ("[" * 1000 + "]" * 1000, "journal cannot be read"),
    ],
)
def test_a_malformed_or_foreign_evidence_gap_marker_refuses_the_report(
    ledger: Path, tmp_path: Path, data: str, rule: str
) -> None:
    """The journal is untrusted: every shape arrives as ``ReportError`` and none is echoed.

    A marker bound to another hash is refused, not ignored -- reading it as absent would let
    the record be summarized as though it had had the evidence (tournament.is_evidence_poor).
    """
    _plant_marker(ledger, "rec-03", data)
    with pytest.raises(ReportError, match=rule) as caught:
        write_report(ledger, tmp_path / "out", now=NOW)
    assert CANARY not in str(caught.value)


def test_a_marker_for_the_records_own_hash_is_counted(ledger: Path, tmp_path: Path) -> None:
    conn = connect(ledger)
    (digest,) = conn.execute(
        "SELECT forecast_sha256 FROM forecast_records WHERE record_id = 'rec-03'"
    ).fetchone()
    append(conn, "evidence_gap", "rec-03", {"code": "evidence_poor", "forecast_sha256": digest})
    conn.close()
    write_report(ledger, tmp_path / "out", now=NOW)
    (row,) = [row for row in _records(tmp_path / "out") if row["record_id"] == "rec-03"]
    assert row["evidence_gaps"] == ["evidence_poor"]


def test_an_overflowing_summary_is_refused_and_leaves_no_files(tmp_path: Path) -> None:
    """Reachable: 017 admits any finite double, and two maxima overflow their sum."""
    extreme = sys.float_info.max
    specs = (
        Spec("big-1", 201, observations=(("yes", extreme, True),)),
        Spec("big-2", 202, observations=(("yes", extreme, True),)),
    )
    path = build_ledger(tmp_path / "ledger.sqlite3", specs)
    with pytest.raises(ReportError, match="not representable") as caught:
        write_report(path, tmp_path / "out", now=NOW)
    assert "e+308" not in str(caught.value)
    assert not (tmp_path / "out").exists()


def test_an_existing_report_is_never_overwritten(template: Path, tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / RECORDS_FILENAME).write_bytes(b"earlier")
    with pytest.raises(ReportError):
        write_report(template, destination, now=NOW)
    assert (destination / RECORDS_FILENAME).read_bytes() == b"earlier"
    assert not (destination / MANIFEST_FILENAME).exists()


def test_a_missing_ledger_is_refused_and_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(LedgerError):
        write_report(missing, tmp_path / "out", now=NOW)
    assert not missing.exists()


def test_a_file_that_is_not_a_database_is_refused_as_a_ledger_error(tmp_path: Path) -> None:
    path = tmp_path / "not-a-db.sqlite3"
    path.write_bytes(b"x" * 4096)
    with pytest.raises(LedgerError):
        write_report(path, tmp_path / "out", now=NOW)


def test_a_ledger_behind_this_build_is_refused_and_not_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.sqlite3"
    packaged = ledger_module._load_migrations
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger_module, "_load_migrations", lambda: [m for m in packaged() if m[0] <= 16]
        )
        initialize_ledger(path)
    with pytest.raises(LedgerError):
        write_report(path, tmp_path / "out", now=NOW)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (16,)
    finally:
        conn.close()


def test_an_excluded_record_never_supersedes_an_included_one(tmp_path: Path) -> None:
    """Round 1's finding, reproduced through the production writers.

    Both records forecast question 2001 and both were posted and scored; `rec-z` is in a test
    tournament and sorts after `rec-a`. Choosing the subject across both populations made
    `rec-z` the subject and `rec-a` `superseded`, so the included population reported no
    scored record, no score cell and no calibration point for a verified outcome.
    """
    path = build_ledger(
        tmp_path / "ledger.sqlite3",
        (
            Spec("rec-a", 2001, probability_yes=0.6, observations=(("yes", 3.0, True),)),
            Spec(
                "rec-z",
                2001,
                probability_yes=0.6,
                tournament_id="bot-testing-area",
                observations=(("yes", 9.0, True),),
            ),
        ),
    )
    write_report(path, tmp_path / "out", now=NOW)
    rows = {row["record_id"]: row for row in _records(tmp_path / "out")}
    assert (rows["rec-a"]["included"], rows["rec-a"]["state"]) == (True, "scored")
    assert (rows["rec-z"]["included"], rows["rec-z"]["state"]) == (False, "scored")
    report = _report(tmp_path / "out")
    assert report["population"]["states"]["scored"] == 1
    everything = _group(report, "all", {})
    cell = _cell(everything, "platform_spot_peer_score")
    assert (cell["n"], cell["sum"]) == (1, 3.0)
    assert everything["calibration"]["n"] == 1


def test_every_spec_record_is_in_the_fixture() -> None:
    """The fixture table and the expected partition name the same records."""
    assert {spec.record_id for spec in SPECS} == set(EXPECTED_STATES)


def test_an_unposted_later_version_does_not_supersede_the_posted_one(tmp_path: Path) -> None:
    """Only a *posted* later version supersedes: an unposted v2 leaves v1 the subject."""
    path = tmp_path / "ledger.sqlite3"
    initialize_ledger(path)
    conn = connect(path)
    try:
        seed(conn, Spec("solo", 301, observations=(("yes", 1.0, True),)))
        seed(
            conn,
            Spec(
                "solo-v2",
                301,
                forecast_version=2,
                parent_record_id="solo",
                posted=False,
            ),
        )
    finally:
        conn.close()
    write_report(path, tmp_path / "out", now=NOW)
    states = {row["record_id"]: row["state"] for row in _records(tmp_path / "out")}
    assert states == {"solo": "scored", "solo-v2": "not_posted"}


def test_the_generated_report_directory_holds_exactly_three_files(generated: Path) -> None:
    assert sorted(path.name for path in generated.iterdir()) == sorted(
        [RECORDS_FILENAME, REPORT_FILENAME, MANIFEST_FILENAME]
    )


def test_copying_a_report_directory_is_not_needed_to_replay_it(
    generated: Path, tmp_path: Path
) -> None:
    """The report stands alone: its JSON re-renders canonically to the same bytes."""
    shutil.copytree(generated, tmp_path / "copy")
    text = (tmp_path / "copy" / REPORT_FILENAME).read_text("utf-8")
    rendered = json.dumps(
        json.loads(text), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    assert f"{rendered}\n" == text
