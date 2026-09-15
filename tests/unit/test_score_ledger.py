"""M4-802: the local score writer, its reader, and ``015_local_score_events.sql``.

Expected values are worked by hand beside each assertion; logs are ``bc -l`` literals shared
with ``test_scoring.py``. The schema half drives every trigger clause by raw SQL with a row
that satisfies 014 and every *other* 015 clause, so each refusal is that clause's own.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from resolution_rows import insert_resolution_row, insert_score_row, seed_submitted
from score_rows import RESOLVED_AT, SCORED_AT, resolve, seed_resolved
from whiskeyjack_bot import ledger as ledger_module
from whiskeyjack_bot import lifecycle, scoring
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, LedgerError, connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    current_status,
    latest_resolution,
    read_history,
    read_local_scores,
    record_local_scores,
)

# ln(x) from `echo "scale=25; l(x)" | bc -l`, as in test_scoring.py.
LN_0_7 = -0.3566749439387323789126387
LN_0_3 = -1.2039728043259359926227462
LN_0_5 = -0.6931471805599453094172321

QUESTION_ID = 45747
POST_ID = 45556


def close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0.0)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    connection = connect(db)
    try:
        yield connection
    finally:
        connection.close()


def _count(conn: sqlite3.Connection, table: str = "score_events") -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _binary(conn: sqlite3.Connection, record_id: str = "rec-b", **kwargs: object) -> str:
    return seed_resolved(conn, record_id, question_id=QUESTION_ID, post_id=POST_ID, **kwargs)


def _mc(conn: sqlite3.Connection, record_id: str = "rec-mc", **kwargs: object) -> str:
    return seed_resolved(
        conn,
        record_id,
        question_id=QUESTION_ID + 1,
        post_id=POST_ID + 1,
        question_type="multiple_choice",
        **kwargs,
    )


# ── the writer ───────────────────────────────────────────────────────────────


def test_a_resolved_binary_record_is_scored_and_moves_to_scored_atomically(
    conn: sqlite3.Connection,
) -> None:
    record = _binary(conn, probability_yes=0.7)  # the fixture resolves "yes"
    resolution = latest_resolution(conn, record)
    assert resolution is not None and resolution.observation.outcome == "yes"
    assert current_status(conn, record) == "resolved"

    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)

    assert write.outcome == "appended" and write.resolution == resolution
    assert [(s.metric, s.implementation_version) for s in write.scores] == [
        ("local_brier_binary", "local_brier_binary/1"),
        ("local_log_binary", "local_log_binary/1"),
    ]
    assert close(write.scores[0].value, 0.09)  # (0.7 - 1)^2
    assert close(write.scores[1].value, LN_0_7)
    assert {s.resolution_event_id for s in write.scores} == {resolution.event_id}
    assert {s.computed_at_utc for s in write.scores} == {"2026-09-18T09:00:00.000000+00:00"}
    assert write.event is not None and write.event.event_type == "scored"
    assert write.event.score_event_id == write.scores[0].event_id
    assert current_status(conn, record) == "scored"
    assert (
        conn.execute(
            "SELECT count(*) FROM score_events WHERE comparison_baseline IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


def test_a_resolved_multiple_choice_record_is_scored_by_label(conn: sqlite3.Connection) -> None:
    # Options (Alpha 0.2, Beta 0.5, Other 0.3); the fixture resolves "Option Beta".
    # Brier: 0.2^2 + (0.5 - 1)^2 + 0.3^2 = 0.04 + 0.25 + 0.09 = 0.38. Log: ln(0.5).
    record = _mc(conn)
    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert [s.metric for s in write.scores] == ["local_brier_multiclass", "local_log_multiclass"]
    assert close(write.scores[0].value, 0.38)
    assert close(write.scores[1].value, LN_0_5)
    assert current_status(conn, record) == "scored"


def test_the_stored_option_order_does_not_change_the_score(conn: sqlite3.Connection) -> None:
    """The same forecast stored in a different option order scores identically, to the bit."""
    forward = _mc(
        conn, "rec-forward", options=(("Option Alpha", 0.2), ("Option Beta", 0.5), ("Other", 0.3))
    )
    backward = seed_resolved(
        conn,
        "rec-backward",
        question_id=QUESTION_ID + 2,
        post_id=POST_ID + 2,
        question_type="multiple_choice",
        options=(("Other", 0.3), ("Option Beta", 0.5), ("Option Alpha", 0.2)),
    )
    a = record_local_scores(conn, record_id=forward, computed_at=SCORED_AT).scores
    b = record_local_scores(conn, record_id=backward, computed_at=SCORED_AT).scores
    assert [s.value for s in a] == [s.value for s in b]
    assert close(b[0].value, 0.38)


def test_scoring_again_writes_nothing(conn: sqlite3.Connection) -> None:
    record = _binary(conn)
    record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    history = read_history(conn, record)
    again = record_local_scores(conn, record_id=record, computed_at=SCORED_AT + timedelta(days=1))
    assert again.outcome == "unchanged" and again.scores == () and again.event is None
    assert _count(conn) == 2
    assert read_history(conn, record) == history


def test_a_record_with_no_resolution_is_not_scorable(conn: sqlite3.Connection) -> None:
    from score_rows import seed_forecast, walk_to_submitted

    digest = seed_forecast(conn, "rec-open", question_id=QUESTION_ID, post_id=POST_ID)
    walk_to_submitted(conn, "rec-open", digest)
    write = record_local_scores(conn, record_id="rec-open", computed_at=SCORED_AT)
    assert (write.outcome, write.resolution, write.event) == ("not_scorable", None, None)
    assert _count(conn) == 0 and current_status(conn, "rec-open") == "submitted"


@pytest.mark.parametrize("resolution", ["annulled", "ambiguous", None])
def test_a_record_whose_latest_resolution_is_not_resolved_is_not_scorable(
    conn: sqlite3.Connection, resolution: str | None
) -> None:
    record = _binary(conn, resolution=resolution)  # None on a resolved question = withheld
    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert write.outcome == "not_scorable" and write.resolution is not None
    assert not write.resolution.scorable
    assert _count(conn) == 0 and current_status(conn, record) != "scored"


def test_a_retraction_after_scoring_stops_further_scores_and_keeps_the_old_ones(
    conn: sqlite3.Connection,
) -> None:
    record = _binary(conn)
    record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    resolve(
        conn, record, status="closed", resolution=None, observed_at=SCORED_AT + timedelta(hours=1)
    )
    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT + timedelta(hours=2))
    assert write.outcome == "not_scorable"
    assert _count(conn) == 2 and current_status(conn, record) == "scored"


def test_a_re_resolution_after_scoring_appends_new_rows_and_no_lifecycle_event(
    conn: sqlite3.Connection,
) -> None:
    """Never an UPDATE: the "yes" rows stay, "no" rows are added against the new observation."""
    record = _binary(conn, probability_yes=0.7)
    first = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    resolve(conn, record, resolution="no", observed_at=SCORED_AT + timedelta(hours=1))
    events = len(read_history(conn, record))

    second = record_local_scores(conn, record_id=record, computed_at=SCORED_AT + timedelta(hours=2))

    assert second.outcome == "appended" and second.event is None
    assert close(second.scores[0].value, 0.49)  # (0.7 - 0)^2
    assert close(second.scores[1].value, LN_0_3)
    new_resolution = latest_resolution(conn, record)
    assert new_resolution is not None
    assert {s.resolution_event_id for s in second.scores} == {new_resolution.event_id}
    assert new_resolution.event_id != first.scores[0].resolution_event_id
    assert len(read_history(conn, record)) == events and current_status(conn, record) == "scored"
    assert [s.event_id for s in read_local_scores(conn, record)] == [
        *(s.event_id for s in first.scores),
        *(s.event_id for s in second.scores),
    ]


def test_a_return_to_an_earlier_outcome_is_scored_again_against_the_new_row(
    conn: sqlite3.Connection,
) -> None:
    record = _binary(conn)
    record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    resolve(conn, record, resolution="annulled", observed_at=SCORED_AT + timedelta(hours=1))
    resolve(conn, record, resolution="yes", observed_at=SCORED_AT + timedelta(hours=2))
    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT + timedelta(hours=3))
    assert write.outcome == "appended" and len(write.scores) == 2
    assert _count(conn) == 4


def test_a_new_implementation_version_appends_only_its_own_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code change that bumps one metric's version. Simulated by registering a /2."""
    record = _binary(conn)
    record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    versions = dict(scoring.IMPLEMENTATION_VERSIONS)
    versions["local_brier_binary"] = "local_brier_binary/2"
    registry = dict(scoring._IMPLEMENTATIONS)
    registry["local_brier_binary/2"] = ("local_brier_binary", scoring.binary_brier_v1)
    monkeypatch.setattr(scoring, "IMPLEMENTATION_VERSIONS", versions)
    monkeypatch.setattr(scoring, "_IMPLEMENTATIONS", registry)

    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT + timedelta(hours=1))

    assert [s.implementation_version for s in write.scores] == ["local_brier_binary/2"]
    assert write.event is None and _count(conn) == 3
    assert len(read_local_scores(conn, record)) == 3


def test_a_lifecycle_refusal_leaves_no_score_row_behind(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows and the event are one transaction: a failed event rolls the rows back."""
    record = _binary(conn)

    def refuse(*args: object, **kwargs: object) -> object:
        raise LifecycleError("simulated refusal of the scored event")

    monkeypatch.setattr(lifecycle, "_append_event", refuse)
    with pytest.raises(LifecycleError, match="simulated"):
        record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert _count(conn) == 0 and current_status(conn, record) == "resolved"


def test_a_score_computed_before_its_observation_is_refused(conn: sqlite3.Connection) -> None:
    record = _binary(conn)
    with pytest.raises(LifecycleError, match="earlier than the observation"):
        record_local_scores(conn, record_id=record, computed_at=RESOLVED_AT - timedelta(seconds=1))
    assert _count(conn) == 0


def test_a_record_that_does_not_read_back_is_refused_without_its_content(
    conn: sqlite3.Connection,
) -> None:
    """resolution_rows' placeholder record (`record_json = '{}'`) is not a forecast."""
    seed_submitted(conn, "rec-placeholder", question_id=QUESTION_ID, post_id=POST_ID)
    resolve(conn, "rec-placeholder")
    with pytest.raises(LifecycleError, match="cannot be read back") as excinfo:
        record_local_scores(conn, record_id="rec-placeholder", computed_at=SCORED_AT)
    assert excinfo.value.__cause__ is None and _count(conn) == 0


def test_a_scorable_row_on_a_record_that_never_moved_is_refused(conn: sqlite3.Connection) -> None:
    """A raw resolution row with no `resolved` event: the record is still `submitted`."""
    seed_submitted(conn, "rec-raw-status", question_id=QUESTION_ID, post_id=POST_ID)
    insert_resolution_row(conn, "rec-raw-status")
    with pytest.raises(LifecycleError, match="current status is submitted"):
        record_local_scores(conn, record_id="rec-raw-status", computed_at=SCORED_AT)
    assert _count(conn) == 0


@pytest.mark.parametrize("record_id", ["", "   ", None, 7, "rec-unknown"])
def test_a_malformed_or_unknown_record_id_is_refused(
    conn: sqlite3.Connection, record_id: object
) -> None:
    with pytest.raises(LifecycleError):
        record_local_scores(conn, record_id=record_id, computed_at=SCORED_AT)  # type: ignore[arg-type]


def test_a_naive_computed_at_is_refused(conn: sqlite3.Connection) -> None:
    record = _binary(conn)
    with pytest.raises(LifecycleError):
        record_local_scores(conn, record_id=record, computed_at=SCORED_AT.replace(tzinfo=None))


# ── the reader ───────────────────────────────────────────────────────────────


def test_the_reader_returns_what_the_writer_wrote(conn: sqlite3.Connection) -> None:
    record = _binary(conn)
    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert read_local_scores(conn, record) == write.scores
    assert read_local_scores(conn, _mc(conn)) == ()


def test_the_reader_refuses_a_stored_value_that_no_longer_recomputes(
    conn: sqlite3.Connection,
) -> None:
    record = _binary(conn)
    record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    # 003 blocks UPDATE; an operator with a shell can drop the block, so this test does.
    conn.execute("DROP TRIGGER score_events_block_update")
    conn.execute("UPDATE score_events SET value = 0.0900001 WHERE metric = 'local_brier_binary'")
    with pytest.raises(LifecycleError, match="does not match its recomputation") as excinfo:
        read_local_scores(conn, record)
    assert "0.09" not in str(excinfo.value)


def test_the_reader_refuses_a_version_this_build_does_not_register(
    conn: sqlite3.Connection,
) -> None:
    record = _binary(conn)
    record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    insert_score_row(
        conn,
        record,
        metric="local_brier_binary",
        value=0.09,
        implementation_version="local_brier_binary/9",
        computed_at_utc="2026-09-18T10:00:00.000000+00:00",
    )
    with pytest.raises(LifecycleError, match="cannot be recomputed"):
        read_local_scores(conn, record)


def test_the_reader_refuses_a_score_citing_another_records_resolution(
    conn: sqlite3.Connection,
) -> None:
    """015 refuses this at INSERT; with its trigger dropped, the reader still does."""
    record = _binary(conn)
    record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    other = seed_resolved(conn, "rec-other", question_id=QUESTION_ID + 5, post_id=POST_ID + 5)
    other_resolution = latest_resolution(conn, other)
    assert other_resolution is not None
    conn.execute("DROP TRIGGER score_events_validate_local_score_on_insert")
    insert_score_row(
        conn,
        record,
        value=0.09,  # the right number for "yes" at 0.7, so only the citation is wrong
        implementation_version="local_brier_binary/1",
        resolution_event_id=other_resolution.event_id,
    )
    with pytest.raises(LifecycleError, match="cites a resolution row this record does not have"):
        read_local_scores(conn, record)


def test_the_reader_leaves_rows_of_other_metrics_alone(conn: sqlite3.Connection) -> None:
    """M4-803's platform rows will share the table once it widens 015's vocabulary. Simulated
    by dropping the trigger: such a row is not this reader's to recompute or refuse."""
    record = _binary(conn)
    write = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    conn.execute("DROP TRIGGER score_events_validate_local_score_on_insert")
    insert_score_row(
        conn, record, metric="platform_peer", value=12.5, implementation_version="platform_peer/1"
    )
    assert read_local_scores(conn, record) == write.scores


# ── 015: what a score row may claim ──────────────────────────────────────────


@pytest.fixture
def scorable(conn: sqlite3.Connection) -> str:
    """A placeholder record with one scorable resolution: enough for the triggers."""
    seed_submitted(conn, "rec-raw", question_id=QUESTION_ID, post_id=POST_ID)
    insert_resolution_row(conn, "rec-raw")
    return "rec-raw"


def test_a_well_formed_local_score_row_is_accepted(conn: sqlite3.Connection, scorable: str) -> None:
    insert_score_row(conn, scorable)
    # REAL affinity stores an integer 0 as a real, so a perfect score written as 0 is fine.
    insert_score_row(
        conn,
        scorable,
        metric="local_log_binary",
        value=0,
        implementation_version="local_log_binary/1",
    )
    assert [tuple(row) for row in conn.execute("SELECT typeof(value) FROM score_events")] == [
        ("real",),
        ("real",),
    ]


# "1" is not here: INTEGER affinity stores it as 1, which is a valid citation.
@pytest.mark.parametrize("cited", [None, "one", 1.5, 999_999])
def test_a_score_must_cite_a_resolution_row_of_its_record(
    conn: sqlite3.Connection, scorable: str, cited: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="must name a resolution row"):
        insert_score_row(conn, scorable, resolution_event_id=cited)


def test_a_score_cannot_cite_another_records_resolution(
    conn: sqlite3.Connection, scorable: str
) -> None:
    seed_submitted(conn, "rec-other", question_id=QUESTION_ID + 9, post_id=POST_ID + 9)
    other = insert_resolution_row(conn, "rec-other")
    with pytest.raises(sqlite3.IntegrityError, match="must name a resolution row"):
        insert_score_row(conn, scorable, resolution_event_id=other)


def test_a_score_cannot_cite_a_superseded_observation(
    conn: sqlite3.Connection, scorable: str
) -> None:
    """Latest is scorable (so 014 passes), but the row cites the earlier `resolved` one."""
    first = conn.execute("SELECT max(event_id) FROM resolution_events").fetchone()[0]
    insert_resolution_row(
        conn, scorable, kind="annulled", observed_at_utc="2026-09-17T19:00:00.000000+00:00"
    )
    insert_resolution_row(
        conn, scorable, kind="resolved", observed_at_utc="2026-09-17T20:00:00.000000+00:00"
    )
    with pytest.raises(sqlite3.IntegrityError, match="must be the latest"):
        insert_score_row(conn, scorable, resolution_event_id=first)
    insert_score_row(conn, scorable)


@pytest.mark.parametrize(
    "metric", ["brier", "log", "metaculus_peer", "baseline", "LOCAL_BRIER_BINARY", ""]
)
def test_a_metric_outside_the_local_vocabulary_is_refused(
    conn: sqlite3.Connection, scorable: str, metric: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="not a recognized local score metric"):
        insert_score_row(conn, scorable, metric=metric, implementation_version=f"{metric}/1")


def test_a_blob_metric_is_refused(conn: sqlite3.Connection, scorable: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="not a recognized local score metric"):
        insert_score_row(conn, scorable, metric=b"local_brier_binary")


@pytest.mark.parametrize("metric", ["local_brier_multiclass", "local_log_multiclass"])
def test_a_multiclass_metric_does_not_apply_to_a_binary_record(
    conn: sqlite3.Connection, scorable: str, metric: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="does not apply"):
        insert_score_row(
            conn,
            scorable,
            metric=metric,
            value=-0.5 if "log" in metric else 0.5,
            implementation_version=f"{metric}/1",
        )


def test_a_binary_metric_does_not_apply_to_a_multiple_choice_record(
    conn: sqlite3.Connection,
) -> None:
    seed_submitted(
        conn,
        "rec-mc-raw",
        question_id=QUESTION_ID + 3,
        post_id=POST_ID + 3,
        question_type="multiple_choice",
    )
    insert_resolution_row(conn, "rec-mc-raw")
    with pytest.raises(sqlite3.IntegrityError, match="does not apply"):
        insert_score_row(
            conn,
            "rec-mc-raw",
            metric="local_brier_binary",
            implementation_version="local_brier_binary/1",
        )
    insert_score_row(conn, "rec-mc-raw")  # the helper picks local_brier_multiclass


@pytest.mark.parametrize(
    "value", [float("inf"), float("-inf"), float("nan"), None, "0.25x", b"\x00"]
)
def test_a_value_that_is_not_a_finite_real_is_refused(
    conn: sqlite3.Connection, scorable: str, value: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="finite real number"):
        insert_score_row(conn, scorable, value=value)


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("local_brier_binary", -1e-12),
        ("local_brier_binary", 1.0000001),
        ("local_log_binary", 1e-12),
    ],
)
def test_a_value_outside_its_metrics_range_is_refused(
    conn: sqlite3.Connection, scorable: str, metric: str, value: float
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="outside the range"):
        insert_score_row(
            conn, scorable, metric=metric, value=value, implementation_version=f"{metric}/1"
        )


def test_the_range_bounds_are_inclusive(conn: sqlite3.Connection, scorable: str) -> None:
    insert_score_row(conn, scorable, value=0.0)
    insert_score_row(conn, scorable, value=1.0, implementation_version="local_brier_binary/2")
    insert_score_row(
        conn,
        scorable,
        metric="local_log_binary",
        value=-744.44,
        implementation_version="local_log_binary/1",
    )


@pytest.mark.parametrize(
    "version",
    [
        "v1",
        "1",
        "local_brier_binary",
        "local_brier_binary/",
        "local_log_binary/1",
        None,
        "local_brier_binary/" + "x" * 190,
    ],
)
def test_an_implementation_version_must_name_its_metric(
    conn: sqlite3.Connection, scorable: str, version: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="must name its metric"):
        insert_score_row(conn, scorable, implementation_version=version)


@pytest.mark.parametrize("baseline", ["metaculus_peer", "community", ""])
def test_a_local_score_carries_no_comparison_baseline(
    conn: sqlite3.Connection, scorable: str, baseline: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="no comparison baseline"):
        insert_score_row(conn, scorable, comparison_baseline=baseline)


@pytest.mark.parametrize(
    "computed",
    [
        "now",
        "2026-09-18T09:00:00+00:00",
        "2026-09-18 09:00:00.000000+00:00",
        "2026-09-17T17:59:59.999999+00:00",  # one microsecond before the observation
        None,
    ],
)
def test_computed_at_must_be_canonical_and_not_before_the_observation(
    conn: sqlite3.Connection, scorable: str, computed: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="canonical UTC timestamp no earlier"):
        insert_score_row(conn, scorable, computed_at_utc=computed)


def test_one_value_per_observation_metric_and_version(
    conn: sqlite3.Connection, scorable: str
) -> None:
    insert_score_row(conn, scorable)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        insert_score_row(conn, scorable, value=0.5)
    insert_score_row(conn, scorable, implementation_version="local_brier_binary/2")
    assert _count(conn) == 2


def test_a_score_row_is_still_append_only(conn: sqlite3.Connection, scorable: str) -> None:
    insert_score_row(conn, scorable)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE score_events SET resolution_event_id = NULL")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM score_events")


# ── upgrade ──────────────────────────────────────────────────────────────────


def _ledger_at_014(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packaged = ledger_module._load_migrations
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger_module, "_load_migrations", lambda: [m for m in packaged() if m[0] <= 14]
        )
        assert initialize_ledger(db) == 14


def test_a_ledger_at_014_with_resolutions_upgrades_to_015(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ledger.sqlite3"
    _ledger_at_014(db, monkeypatch)
    connection = connect(db)
    try:
        record = _binary(connection)
    finally:
        connection.close()
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 15
    connection = connect(db)
    try:
        write = record_local_scores(connection, record_id=record, computed_at=SCORED_AT)
        assert write.outcome == "appended"
        assert not connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'migration_015%'"
        ).fetchall()
    finally:
        connection.close()


def test_the_migration_refuses_a_ledger_already_holding_score_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ledger.sqlite3"
    _ledger_at_014(db, monkeypatch)
    connection = connect(db)
    try:
        seed_submitted(connection, "rec-old", question_id=QUESTION_ID, post_id=POST_ID)
        insert_resolution_row(connection, "rec-old")
        connection.execute(
            "INSERT INTO score_events (forecast_record_id, metric, value, "
            "implementation_version, computed_at_utc) VALUES ('rec-old', 'brier', 0.25, 'v1', ?)",
            ("2026-09-18T00:00:00.000000+00:00",),
        )
    finally:
        connection.close()
    with pytest.raises(LedgerError, match="failed to apply ledger migration 15"):
        initialize_ledger(db)
    connection = connect(db)
    try:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 14
        columns = {row[1] for row in connection.execute("PRAGMA table_info(score_events)")}
        assert "resolution_event_id" not in columns, "a refused upgrade must leave no column"
    finally:
        connection.close()
