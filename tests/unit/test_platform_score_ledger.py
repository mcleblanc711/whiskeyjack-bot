"""M4-803: Metaculus's own scores in the ledger -- writer, reader, and 017's trigger.

The writer is driven through real observations: a record is posted, the production resolution
writer records a post payload, and :func:`lifecycle.record_platform_scores` reads the scores
back out of what was stored. The trigger is driven by raw INSERTs against a real resolution
row, one clause per test, so each refusal is shown to be that clause's.
"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from resolution_rows import (
    FIXTURES,
    OBSERVED_AT,
    SCORE_DATA,
    insert_resolution_row,
    insert_score_row,
    kind_payload,
    post_payload,
    seed_submitted,
)
from score_rows import SCORED_AT, seed_resolved

import whiskeyjack_bot.ledger as ledger_module
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    current_status,
    read_history,
    read_local_scores,
    read_platform_scores,
    record_local_scores,
    record_platform_scores,
    record_resolution_observation,
)
from whiskeyjack_bot.platform_scores import (
    COMPARISON_BASELINES,
    IMPLEMENTATION_VERSIONS,
    PLATFORM_METRIC_ORDER,
    SCORE_DATA_KEYS,
)
from whiskeyjack_bot.resolution import canonical_json, sha256_text

QUESTION_ID = 45747
POST_ID = 45556
RESOLVED_AT = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
TYPES = ("binary", "multiple_choice", "numeric", "discrete")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    connection = connect(db)
    try:
        yield connection
    finally:
        connection.close()


def _observe(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_type: str = "numeric",
    question_id: int = QUESTION_ID,
    post_id: int = POST_ID,
    payload: dict[str, Any] | None = None,
    **payload_kwargs: Any,
) -> str:
    """A posted record whose question the production writer observed as ``payload``."""
    seed_submitted(
        conn, record_id, question_id=question_id, post_id=post_id, question_type=question_type
    )
    if payload is None:
        payload = post_payload(
            question_type, post_id=post_id, question_id=question_id, **payload_kwargs
        )
    record_resolution_observation(
        conn, record_id=record_id, source_response=payload, observed_at=RESOLVED_AT
    )
    return record_id


# ── the writer ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("question_type", TYPES)
def test_every_type_records_the_four_platform_scores_it_was_observed_with(
    conn: sqlite3.Connection, question_type: str
) -> None:
    record = _observe(conn, "rec-p", question_type=question_type)
    write = record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)

    assert write.outcome == "appended"
    assert [s.metric for s in write.scores] == list(PLATFORM_METRIC_ORDER)
    for score in write.scores:
        assert score.value == SCORE_DATA[SCORE_DATA_KEYS[score.metric]]
        assert score.comparison_baseline == COMPARISON_BASELINES[score.metric]
        assert score.implementation_version == f"{score.metric}/metaculus_score_data/1"
        assert score.resolution_event_id == write.resolution.event_id  # type: ignore[union-attr]
    # The platform writer is what moves a record with no local score; for a record here it
    # links the first row it wrote, the tournament's own default score.
    assert write.event is not None and write.event.score_event_id == write.scores[0].event_id
    assert write.scores[0].metric == "platform_spot_peer_score"
    assert current_status(conn, record) == "scored"
    assert read_platform_scores(conn, record) == write.scores
    assert read_local_scores(conn, record) == ()

    again = record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert (again.outcome, again.scores, again.event) == ("unchanged", (), None)
    assert conn.execute("SELECT count(*) FROM score_events").fetchone()[0] == 4


def test_a_binary_record_scored_locally_keeps_its_event_on_the_local_row(
    conn: sqlite3.Connection,
) -> None:
    record = seed_resolved(conn, "rec-b", question_id=QUESTION_ID, post_id=POST_ID)
    local = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    platform = record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert local.event is not None and platform.event is None
    assert platform.outcome == "appended" and len(platform.scores) == 4
    scored = [e for e in read_history(conn, record) if e.event_type == "scored"]
    assert [e.score_event_id for e in scored] == [local.scores[0].event_id]


@pytest.mark.parametrize("kind", ["annulled", "ambiguous", "withheld"])
def test_an_observation_that_is_not_scorable_writes_nothing(
    conn: sqlite3.Connection, kind: str
) -> None:
    record = _observe(
        conn,
        "rec-n",
        payload=kind_payload("numeric", kind, post_id=POST_ID, question_id=QUESTION_ID),
    )
    write = record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert (write.outcome, write.scores, write.event) == ("not_scorable", (), None)


def test_a_record_with_no_observation_is_not_scorable(conn: sqlite3.Connection) -> None:
    seed_submitted(conn, "rec-0", question_id=QUESTION_ID, post_id=POST_ID)
    write = record_platform_scores(conn, record_id="rec-0", computed_at=SCORED_AT)
    assert (write.outcome, write.resolution) == ("not_scorable", None)


def _drop(key: str) -> Any:
    data = dict(SCORE_DATA)
    del data[key]
    return data


def _with(key: str, value: object) -> Any:
    data: dict[str, object] = dict(SCORE_DATA)
    data[key] = value
    return data


# Owner decision 2026-09-23: a definite resolution whose scores are absent or unreadable is a
# failure, never "nothing to record". Each shape here is one a lax reader would map to quiet.
@pytest.mark.parametrize(
    ("score_data", "rule"),
    [
        (None, "carries no platform scores"),
        ({}, "carries no platform scores"),
        ("SENTINEL-text", "must be a JSON object"),
        (["SENTINEL-list"], "must be a JSON object"),
        (_drop("peer_score"), "has no peer_score"),
        (_drop("spot_peer_score"), "has no spot_peer_score"),
        (_with("baseline_score", 3), "baseline_score must be a float"),
        (_with("spot_baseline_score", True), "spot_baseline_score must be a float"),
        (_with("peer_score", "SENTINEL-12.5"), "peer_score must be a float"),
        (_with("spot_peer_score", None), "spot_peer_score must be a float"),
    ],
)
def test_a_scorable_observation_without_readable_scores_fails_loudly(
    conn: sqlite3.Connection, score_data: object, rule: str
) -> None:
    record = _observe(conn, "rec-x", score_data=score_data)
    with pytest.raises(LifecycleError, match=rule) as excinfo:
        record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert "SENTINEL" not in str(excinfo.value)
    assert current_status(conn, record) == "resolved"
    assert conn.execute("SELECT count(*) FROM score_events").fetchone()[0] == 0


@pytest.mark.parametrize("my_forecasts", ["missing", None, "SENTINEL"])
def test_a_question_without_a_my_forecasts_object_fails_loudly(
    conn: sqlite3.Connection, my_forecasts: object
) -> None:
    payload = post_payload("numeric", post_id=POST_ID, question_id=QUESTION_ID)
    if my_forecasts == "missing":
        del payload["question"]["my_forecasts"]
    else:
        payload["question"]["my_forecasts"] = my_forecasts
    record = _observe(conn, "rec-m", payload=payload)
    with pytest.raises(LifecycleError, match="no my_forecasts object") as excinfo:
        record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert "SENTINEL" not in str(excinfo.value)


def test_a_non_finite_stored_score_fails_loudly(conn: sqlite3.Connection) -> None:
    """`1e999` parses to an infinity. `canonical_json` cannot write one, so the row is planted
    raw with a digest that matches its text: a stored value is untrusted either way."""
    seed_submitted(
        conn, "rec-inf", question_id=QUESTION_ID, post_id=POST_ID, question_type="numeric"
    )
    columns = _resolution_text_row(conn, "rec-inf", "spot_peer_score", "1e999")
    _insert_resolution(conn, columns)
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, resolution_event_id, occurred_at_utc, created_at_utc) "
        "SELECT 'rec-inf', max(event_seq) + 1, 'resolved', 'submitted', 'resolved', "
        "(SELECT max(event_id) FROM resolution_events), ?, ? FROM lifecycle_events "
        "WHERE forecast_record_id = 'rec-inf'",
        (OBSERVED_AT, OBSERVED_AT),
    )
    with pytest.raises(LifecycleError, match="spot_peer_score must be finite"):
        record_platform_scores(conn, record_id="rec-inf", computed_at=SCORED_AT)


def _group_payload(scores: list[dict[str, float]]) -> tuple[dict[str, Any], list[int]]:
    post = json.loads((FIXTURES / "group" / "minibench_group.json").read_text(encoding="utf-8"))
    members = post["group_of_questions"]["questions"]
    for member, data in zip(members, scores, strict=True):
        member["status"] = "resolved"
        member["resolution"] = "yes"
        member["actual_resolve_time"] = "2026-09-17T12:00:00Z"
        member["resolution_set_time"] = "2026-09-17T16:30:00.123456Z"
        member["my_forecasts"] = {"history": [], "score_data": data}
    return post, [member["id"] for member in members]


def _scaled(factor: float) -> dict[str, float]:
    return {key: value * factor for key, value in SCORE_DATA.items()}


def test_a_group_member_is_scored_from_its_own_entry(conn: sqlite3.Connection) -> None:
    post, question_ids = _group_payload([_scaled(1.0), _scaled(2.0), _scaled(3.0)])
    for index, question_id in enumerate(question_ids):
        _observe(
            conn,
            f"rec-g{index}",
            question_type="binary",
            question_id=question_id,
            post_id=post["id"],
            payload=copy.deepcopy(post),
        )
    for index in range(3):
        write = record_platform_scores(conn, record_id=f"rec-g{index}", computed_at=SCORED_AT)
        peer = next(s for s in write.scores if s.metric == "platform_peer_score")
        assert peer.value == SCORE_DATA["peer_score"] * (index + 1.0)


# ── 017: what a platform row may claim ───────────────────────────────────────


@pytest.fixture
def scorable(conn: sqlite3.Connection) -> str:
    """A placeholder numeric record with one scorable observation carrying SCORE_DATA."""
    seed_submitted(
        conn, "rec-raw", question_id=QUESTION_ID, post_id=POST_ID, question_type="numeric"
    )
    insert_resolution_row(conn, "rec-raw")
    return "rec-raw"


def _platform_row(
    conn: sqlite3.Connection,
    record_id: str,
    metric: str = "platform_peer_score",
    **overrides: object,
) -> int:
    """Raw-INSERT a platform row that satisfies 014 and 017, with any column overridden."""
    values: dict[str, object] = {
        "metric": metric,
        "value": SCORE_DATA[SCORE_DATA_KEYS[metric]],  # type: ignore[index]
        "implementation_version": IMPLEMENTATION_VERSIONS[metric],  # type: ignore[index]
        "comparison_baseline": COMPARISON_BASELINES[metric],  # type: ignore[index]
    }
    values.update(overrides)
    return insert_score_row(conn, record_id, **values)


@pytest.mark.parametrize("metric", PLATFORM_METRIC_ORDER)
@pytest.mark.parametrize("question_type", TYPES)
def test_a_well_formed_platform_row_is_accepted_on_every_type(
    conn: sqlite3.Connection, metric: str, question_type: str
) -> None:
    seed_submitted(
        conn, "rec-t", question_id=QUESTION_ID, post_id=POST_ID, question_type=question_type
    )
    insert_resolution_row(conn, "rec-t")
    _platform_row(conn, "rec-t", metric)


@pytest.mark.parametrize(
    ("metric", "baseline"),
    [
        ("platform_peer_score", None),
        ("platform_peer_score", "baseline"),
        ("platform_spot_peer_score", "PEER"),
        ("platform_baseline_score", "peer"),
        ("platform_spot_baseline_score", ""),
    ],
)
def test_a_platform_row_must_carry_its_metrics_baseline(
    conn: sqlite3.Connection, scorable: str, metric: str, baseline: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="must carry its comparison baseline"):
        _platform_row(conn, scorable, metric, comparison_baseline=baseline)


@pytest.mark.parametrize(
    "suffix",
    [
        "1",
        "metaculus_score_data/0",
        "metaculus_score_data/01",
        "metaculus_score_data/",
        "metaculus_score_data/1x",
        "metaculus_score_data/1/",
        "local_formula/1",
        "Metaculus_score_data/1",
    ],
)
def test_a_platform_row_must_name_the_score_data_it_was_read_from(
    conn: sqlite3.Connection, scorable: str, suffix: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="must name the score_data"):
        _platform_row(conn, scorable, implementation_version=f"platform_peer_score/{suffix}")


def test_a_later_extraction_version_is_admitted_by_the_schema(
    conn: sqlite3.Connection, scorable: str
) -> None:
    """The schema pins the shape; which versions exist is `platform_scores`' registry."""
    _platform_row(
        conn, scorable, implementation_version="platform_peer_score/metaculus_score_data/12"
    )


@pytest.mark.parametrize(
    "value",
    [
        math.nextafter(SCORE_DATA["peer_score"], math.inf),
        SCORE_DATA["spot_peer_score"],  # another metric's number
        0.0,
        -SCORE_DATA["peer_score"],
    ],
)
def test_a_platform_row_must_hold_the_value_its_observation_holds(
    conn: sqlite3.Connection, scorable: str, value: float
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="the value its cited observation holds"):
        _platform_row(conn, scorable, value=value)


@pytest.mark.parametrize("score_data", [{}, None, {"peer_score": 3}, {"peer_score": "3.5"}])
def test_an_observation_without_a_real_score_admits_no_platform_row(
    conn: sqlite3.Connection, score_data: object
) -> None:
    """An integer JSON score is refused too: SQLite's `=` would admit 3 against 3.0, and the
    Python reader refuses it, so the schema must not be laxer than the reader."""
    seed_submitted(conn, "rec-e", question_id=QUESTION_ID, post_id=POST_ID, question_type="numeric")
    payload = post_payload(
        "numeric", post_id=POST_ID, question_id=QUESTION_ID, score_data=score_data
    )
    columns = _payload_row(conn, "rec-e", payload)
    _insert_resolution(conn, columns)
    for value in (3.0, 3.5):
        with pytest.raises(sqlite3.IntegrityError, match="the value its cited observation holds"):
            _platform_row(conn, "rec-e", value=value)


def test_a_group_row_must_hold_its_own_members_value(conn: sqlite3.Connection) -> None:
    post, question_ids = _group_payload([_scaled(1.0), _scaled(2.0), _scaled(3.0)])
    seed_submitted(conn, "rec-g", question_id=question_ids[1], post_id=post["id"])
    _insert_resolution(conn, _payload_row(conn, "rec-g", post))
    with pytest.raises(sqlite3.IntegrityError, match="the value its cited observation holds"):
        _platform_row(conn, "rec-g", value=SCORE_DATA["peer_score"] * 1.0)
    _platform_row(conn, "rec-g", value=SCORE_DATA["peer_score"] * 2.0)


def test_a_group_listing_the_question_twice_admits_no_platform_row(
    conn: sqlite3.Connection,
) -> None:
    """`select_question` refuses a doubled member; the trigger must not pick one of the two."""
    post, question_ids = _group_payload([_scaled(1.0), _scaled(2.0), _scaled(3.0)])
    seed_submitted(conn, "rec-d", question_id=question_ids[0], post_id=post["id"])
    _insert_resolution(conn, _payload_row(conn, "rec-d", post))
    conn.execute("DROP TRIGGER resolution_events_block_update")
    doubled = copy.deepcopy(post)
    doubled["group_of_questions"]["questions"][1]["id"] = question_ids[0]
    conn.execute(
        "UPDATE resolution_events SET source_response = ? WHERE forecast_record_id = 'rec-d'",
        (canonical_json(doubled),),
    )
    with pytest.raises(sqlite3.IntegrityError, match="the value its cited observation holds"):
        _platform_row(conn, "rec-d", value=SCORE_DATA["peer_score"])


def test_a_platform_row_cannot_cite_a_superseded_observation(
    conn: sqlite3.Connection, scorable: str
) -> None:
    first = conn.execute("SELECT max(event_id) FROM resolution_events").fetchone()[0]
    insert_resolution_row(
        conn, scorable, kind="annulled", observed_at_utc="2026-09-17T19:00:00.000000+00:00"
    )
    insert_resolution_row(
        conn, scorable, kind="resolved", observed_at_utc="2026-09-17T20:00:00.000000+00:00"
    )
    with pytest.raises(sqlite3.IntegrityError, match="must be the latest"):
        _platform_row(conn, scorable, resolution_event_id=first)
    _platform_row(conn, scorable)


@pytest.mark.parametrize(
    "metric", ["local_brier_binary", "local_log_binary", "local_brier_multiclass"]
)
@pytest.mark.parametrize("question_type", ["numeric", "discrete"])
def test_no_local_metric_can_label_a_numeric_or_discrete_score(
    conn: sqlite3.Connection, metric: str, question_type: str
) -> None:
    """D30: nothing but the platform's own number is recorded for a continuous question."""
    seed_submitted(
        conn, "rec-l", question_id=QUESTION_ID, post_id=POST_ID, question_type=question_type
    )
    insert_resolution_row(conn, "rec-l")
    with pytest.raises(sqlite3.IntegrityError, match="does not apply to the forecast record"):
        insert_score_row(conn, "rec-l", metric=metric, implementation_version=f"{metric}/1")


def test_a_local_row_still_may_not_carry_a_baseline(conn: sqlite3.Connection) -> None:
    seed_submitted(conn, "rec-lb", question_id=QUESTION_ID, post_id=POST_ID)
    insert_resolution_row(conn, "rec-lb")
    with pytest.raises(sqlite3.IntegrityError, match="a local score has no comparison baseline"):
        insert_score_row(conn, "rec-lb", comparison_baseline="peer")


def test_a_platform_row_is_idempotent_per_observation(
    conn: sqlite3.Connection, scorable: str
) -> None:
    _platform_row(conn, scorable)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _platform_row(conn, scorable)


# ── the reader ───────────────────────────────────────────────────────────────


def _written(conn: sqlite3.Connection) -> str:
    record = _observe(conn, "rec-r")
    record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)
    return record


def test_the_reader_refuses_a_value_its_observation_does_not_hold(
    conn: sqlite3.Connection,
) -> None:
    """017 refuses this at INSERT; with its trigger (and the UNIQUE index, so a second row
    for the same metric and version can land) dropped, the reader still does."""
    record = _written(conn)
    conn.execute("DROP TRIGGER score_events_validate_on_insert")
    conn.execute("DROP INDEX score_events_one_per_observation_metric_version")
    _platform_row(conn, record, value=1.5)
    with pytest.raises(LifecycleError, match="does not match its cited observation"):
        read_platform_scores(conn, record)


def test_the_reader_refuses_an_unregistered_version(conn: sqlite3.Connection) -> None:
    """The schema admits any `/metaculus_score_data/<n>` holding the right value; the reader
    only trusts a version this build registers."""
    record = _written(conn)
    _platform_row(conn, record, implementation_version="platform_peer_score/metaculus_score_data/2")
    with pytest.raises(LifecycleError, match="not a registered platform version"):
        read_platform_scores(conn, record)


def test_the_reader_refuses_a_row_with_the_wrong_baseline(conn: sqlite3.Connection) -> None:
    record = _written(conn)
    conn.execute("DROP TRIGGER score_events_validate_on_insert")
    conn.execute("DROP INDEX score_events_one_per_observation_metric_version")
    _platform_row(conn, record, comparison_baseline="baseline")
    with pytest.raises(LifecycleError, match="does not carry its metric's baseline"):
        read_platform_scores(conn, record)


def test_the_reader_refuses_a_source_that_no_longer_matches_its_digest(
    conn: sqlite3.Connection,
) -> None:
    record = _written(conn)
    conn.execute("DROP TRIGGER resolution_events_block_update")
    conn.execute("UPDATE resolution_events SET source_response = '{\"SENTINEL\": 1}'")
    with pytest.raises(LifecycleError, match="does not match its digest") as excinfo:
        read_platform_scores(conn, record)
    assert "SENTINEL" not in str(excinfo.value)


def test_the_readers_keep_to_their_own_rows(conn: sqlite3.Connection) -> None:
    record = seed_resolved(conn, "rec-both", question_id=QUESTION_ID, post_id=POST_ID)
    local = record_local_scores(conn, record_id=record, computed_at=SCORED_AT)
    platform = record_platform_scores(conn, record_id=record, computed_at=SCORED_AT)
    assert read_local_scores(conn, record) == local.scores
    assert read_platform_scores(conn, record) == platform.scores


# ── 016 -> 017 ───────────────────────────────────────────────────────────────


def test_a_ledger_at_016_with_local_scores_upgrades_to_017(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ledger.sqlite3"
    packaged = ledger_module._load_migrations
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger_module, "_load_migrations", lambda: [m for m in packaged() if m[0] <= 16]
        )
        assert initialize_ledger(db) == 16
    connection = connect(db)
    try:
        # 017 adds no column, so this build's writers can seed a v16 ledger directly.
        record = seed_resolved(connection, "rec-old", question_id=QUESTION_ID, post_id=POST_ID)
        local = record_local_scores(connection, record_id=record, computed_at=SCORED_AT)
        with pytest.raises(sqlite3.IntegrityError, match="not a recognized local score metric"):
            _platform_row(connection, record)
    finally:
        connection.close()
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 17
    assert initialize_ledger(db) == 17
    connection = connect(db)
    try:
        assert read_local_scores(connection, record) == local.scores
        platform = record_platform_scores(connection, record_id=record, computed_at=SCORED_AT)
        assert platform.outcome == "appended" and platform.event is None
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'score_events'"
            )
        }
        assert "score_events_validate_local_score_on_insert" not in names
        assert "score_events_validate_on_insert" in names
    finally:
        connection.close()


# ── helpers for planting a resolution row from a hand-built payload ──────────


def _payload_row(
    conn: sqlite3.Connection, record_id: str, payload: dict[str, Any]
) -> dict[str, object]:
    from resolution_rows import resolution_columns

    return resolution_columns(conn, record_id, payload=payload)


def _resolution_text_row(
    conn: sqlite3.Connection, record_id: str, key: str, number_text: str
) -> dict[str, object]:
    """A resolved row whose stored source text has ``key`` spelled ``number_text``."""
    columns = _payload_row(
        conn,
        record_id,
        post_payload("numeric", post_id=POST_ID, question_id=QUESTION_ID),
    )
    source = str(columns["source_response"])
    spelled = f'"{key}":{json.dumps(SCORE_DATA[key])}'
    assert source.count(spelled) == 1
    source = source.replace(spelled, f'"{key}":{number_text}')
    columns["source_response"] = source
    columns["source_response_sha256"] = sha256_text(source)
    return columns


def _insert_resolution(conn: sqlite3.Connection, columns: dict[str, object]) -> int:
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO resolution_events ({names}) VALUES ({placeholders})",
        tuple(columns.values()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid
