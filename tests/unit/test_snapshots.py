"""M0-103 acceptance: saved snapshots round-trip without network access and
retain question, post, and tournament identity."""

import json
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import (
    BinaryQuestion,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
)

from whiskeyjack_bot.metaculus.snapshots import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotError,
    load_snapshot,
    save_snapshot,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COMMITTED_SNAPSHOT = FIXTURES / "snapshots" / "minibench_sample_snapshot.json"


def load_fixture_questions() -> list[MetaculusQuestion]:
    posts = sorted((FIXTURES / "api_posts").glob("*_post.json"))
    return [
        DataOrganizer.get_question_from_post_json(json.loads(p.read_text(encoding="utf-8")))
        for p in posts
    ]


def test_api_post_fixtures_parse_to_expected_types() -> None:
    questions = load_fixture_questions()
    types = {type(q) for q in questions}
    assert types == {BinaryQuestion, MultipleChoiceQuestion, NumericQuestion}
    for q in questions:
        assert "minibench" in q.tournament_slugs
        assert q.id_of_question is not None
        assert q.id_of_post is not None


def test_fixtures_contain_no_community_prediction() -> None:
    for q in load_fixture_questions():
        if isinstance(q, BinaryQuestion):
            assert q.community_prediction_at_access_time is None


def test_save_and_reload_round_trip(tmp_path: Path) -> None:
    questions = load_fixture_questions()
    path = tmp_path / "snap.json"
    written_meta = save_snapshot(
        path,
        questions,
        tournament_id="minibench",
        group_question_mode="unpack_subquestions",
        source="fixture",
        fetched_at_utc=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )
    meta, reloaded = load_snapshot(path)
    assert meta == written_meta
    assert meta.tournament_id == "minibench"
    assert [q.id_of_question for q in reloaded] == [q.id_of_question for q in questions]
    assert [q.id_of_post for q in reloaded] == [q.id_of_post for q in questions]
    assert [type(q) for q in reloaded] == [type(q) for q in questions]
    # Full content identity, including the retained raw api_json payload.
    assert [q.to_json() for q in reloaded] == [q.to_json() for q in questions]


def test_committed_sample_snapshot_loads() -> None:
    meta, questions = load_snapshot(COMMITTED_SNAPSHOT)
    assert meta.tournament_id == "minibench"
    assert meta.question_count == len(questions) == 3


def test_unknown_schema_version_rejected(tmp_path: Path) -> None:
    envelope = json.loads(COMMITTED_SNAPSHOT.read_text(encoding="utf-8"))
    envelope["snapshot_schema_version"] = "999.0.0"
    path = tmp_path / "bad_version.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SnapshotError, match="schema version"):
        load_snapshot(path)


def test_unknown_question_class_rejected(tmp_path: Path) -> None:
    envelope = json.loads(COMMITTED_SNAPSHOT.read_text(encoding="utf-8"))
    envelope["questions"][0]["question_class"] = "TotallyMadeUpQuestion"
    path = tmp_path / "bad_class.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    # Re-review finding 1 tightened the message: the class name comes from the
    # snapshot file, so it is withheld like every other snapshot value.
    with pytest.raises(SnapshotError, match="unrecognized question_class"):
        load_snapshot(path)


def test_count_mismatch_rejected(tmp_path: Path) -> None:
    envelope = json.loads(COMMITTED_SNAPSHOT.read_text(encoding="utf-8"))
    envelope["question_count"] = 7
    path = tmp_path / "bad_count.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SnapshotError, match="declares 7"):
        load_snapshot(path)


@pytest.mark.parametrize(
    ("description", "mutate", "match"),
    [
        ("questions not a list", lambda e: e.update(questions="not-a-list"), "in a list"),
        ("entry not an object", lambda e: e.update(questions=["not-a-dict"]), "JSON object"),
        (
            "entry missing question_class",
            lambda e: e["questions"][0].pop("question_class"),
            "missing question_class",
        ),
        (
            "entry missing data",
            lambda e: e["questions"][0].pop("data"),
            "missing its data payload",
        ),
        (
            "entry data does not deserialize",
            lambda e: e["questions"][0].update(data={"nonsense": True}),
            "does not deserialize",
        ),
        ("missing metadata key", lambda e: e.pop("tournament_id"), "missing metadata"),
        (
            "bad timestamp",
            lambda e: e.update(fetched_at_utc="not-a-timestamp"),
            "invalid fetched_at_utc",
        ),
        # Re-review finding 2: metadata was only checked for presence; every
        # shape below previously loaded into SnapshotMeta unchallenged.
        ("tournament_id a list", lambda e: e.update(tournament_id=[]), "tournament_id"),
        ("tournament_id a bool", lambda e: e.update(tournament_id=True), "tournament_id"),
        ("tournament_id empty", lambda e: e.update(tournament_id=""), "tournament_id"),
        (
            "group_question_mode a bool",
            lambda e: e.update(group_question_mode=False),
            "group_question_mode",
        ),
        (
            "group_question_mode unknown",
            lambda e: e.update(group_question_mode="merge_everything"),
            "group_question_mode",
        ),
        ("source a list", lambda e: e.update(source=[]), "source"),
        ("source unknown", lambda e: e.update(source="archive"), "source"),
        (
            "timestamp timezone-naive",
            lambda e: e.update(fetched_at_utc="2026-07-10T12:00:00"),
            "timezone-aware",
        ),
        (
            "question_count a string",
            lambda e: e.update(question_count="3"),
            "question_count",
        ),
        (
            "question_count a bool",
            lambda e: e.update(question_count=True),
            "question_count",
        ),
    ],
)
def test_malformed_snapshot_shapes_raise_snapshot_error(
    tmp_path: Path, description: str, mutate: Callable[[dict], object], match: str
) -> None:
    # Cross-review finding 5: each of these shapes previously escaped as a raw
    # AttributeError / KeyError / ValueError; the CLI only handles SnapshotError.
    envelope = json.loads(COMMITTED_SNAPSHOT.read_text(encoding="utf-8"))
    mutate(envelope)
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SnapshotError, match=match):
        load_snapshot(path)


def test_aware_non_utc_timestamp_is_normalized_to_utc(tmp_path: Path) -> None:
    # An aware non-UTC offset is a well-defined instant: accepted, but the
    # loaded provenance is normalized so fetched_at_utc means what it says.
    envelope = json.loads(COMMITTED_SNAPSHOT.read_text(encoding="utf-8"))
    envelope["fetched_at_utc"] = "2026-07-10T14:00:00+02:00"
    path = tmp_path / "offset.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    meta, _ = load_snapshot(path)
    assert meta.fetched_at_utc == datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    assert meta.fetched_at_utc.utcoffset() == timedelta(0)


PLANTED_SECRET = "privateFAKE123456"


@pytest.mark.parametrize(
    ("description", "mutate"),
    [
        (
            "secret in a payload that fails model validation",
            lambda e: e["questions"][0].update(data=json.dumps({"question_text": PLANTED_SECRET})),
        ),
        (
            "secret as the schema version",
            lambda e: e.update(snapshot_schema_version=PLANTED_SECRET),
        ),
        (
            "secret as the question class",
            lambda e: e["questions"][0].update(question_class=PLANTED_SECRET),
        ),
        (
            "secret as the timestamp",
            lambda e: e.update(fetched_at_utc=PLANTED_SECRET),
        ),
    ],
)
def test_snapshot_errors_never_echo_snapshot_contents(
    tmp_path: Path, description: str, mutate: Callable[[dict], object]
) -> None:
    # Re-review finding 1: the deserialization failure interpolated the
    # underlying validation exception (which prints input values) and chained
    # it, so a planted credential surfaced in str(SnapshotError) and in any
    # traceback rendering. Same rule as ConfigError: snapshot-supplied values
    # never appear in the error text or its cause chain.
    envelope = json.loads(COMMITTED_SNAPSHOT.read_text(encoding="utf-8"))
    mutate(envelope)
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SnapshotError) as excinfo:
        load_snapshot(path)
    assert PLANTED_SECRET not in str(excinfo.value), description
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert PLANTED_SECRET not in rendered, description


def test_missing_and_malformed_files_rejected(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(SnapshotError):
        load_snapshot(bad)


def test_schema_version_constant_matches_committed_fixture() -> None:
    envelope = json.loads(COMMITTED_SNAPSHOT.read_text(encoding="utf-8"))
    assert envelope["snapshot_schema_version"] == SNAPSHOT_SCHEMA_VERSION
