"""M1-306: raw provider bodies persist as versioned, non-overwritable artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from whiskeyjack_bot.research.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    ArtifactError,
    artifact_relative_path,
    read_raw_responses,
    write_raw_responses,
)

WHEN = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
BODIES = [{"articles": [{"title": "one"}]}, {"articles": []}]

# Low-entropy on purpose; see docs/LESSONS.md on the gitleaks history scan.
PLANTED = "privateFAKE123456"


def _write(root: Path, **overrides: object) -> str | None:
    kwargs: dict[str, object] = {
        "retrieval_run_id": "run-1",
        "question_id": 42,
        "provider": "asknews",
        "raw_responses": BODIES,
        "written_at_utc": WHEN,
        "retain": True,
    }
    kwargs.update(overrides)
    return write_raw_responses(root, **kwargs)  # type: ignore[arg-type]


def test_an_artifact_round_trips(tmp_path: Path) -> None:
    relative = _write(tmp_path)
    assert relative == "research/42/run-1.json"
    assert read_raw_responses(tmp_path, relative) == tuple(BODIES)


def test_the_recorded_path_is_relative_to_the_artifact_root(tmp_path: Path) -> None:
    """So a ledger stays readable after the artifact directory moves.

    It is also why the path is excluded from the packet hash: an absolute path
    would make a stored digest machine-dependent.
    """
    relative = _write(tmp_path)
    assert relative is not None
    assert not Path(relative).is_absolute()
    moved = tmp_path / "moved"
    moved.mkdir()
    (tmp_path / "research").rename(moved / "research")
    assert read_raw_responses(moved, relative) == tuple(BODIES)


def test_retention_off_writes_nothing_and_records_nothing(tmp_path: Path) -> None:
    """The committed default's meaning, and not a failure."""
    assert _write(tmp_path, retain=False) is None
    assert not (tmp_path / "research").exists()


def test_an_existing_artifact_is_never_overwritten(tmp_path: Path) -> None:
    """An artifact is the record that a paid run happened."""
    _write(tmp_path)
    with pytest.raises(ArtifactError, match="never overwritten"):
        _write(tmp_path, raw_responses=[{"different": True}])
    assert read_raw_responses(tmp_path, "research/42/run-1.json") == tuple(BODIES)


def test_a_run_id_that_would_escape_the_root_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    """A caller mistake, refused before I/O -- not an attacker (see CLAUDE.md).

    A run id carrying a separator would write outside the tree the ledger's
    relative paths are resolved in.
    """
    for run_id in ("../escape", "a/b", ".", "", "x" * 200):
        with pytest.raises(ArtifactError, match="path component"):
            _write(tmp_path, retrieval_run_id=run_id)
    assert list(tmp_path.iterdir()) == []


def test_a_bare_string_of_bodies_is_refused(tmp_path: Path) -> None:
    """``str`` satisfies ``Sequence``; without this it persists one character per body.

    The same caller mistake M1-303's round-4 preflight closed for ``queries``,
    where it cost billable calls.
    """
    with pytest.raises(ArtifactError, match="sequence of JSON objects"):
        _write(tmp_path, raw_responses="not a list of bodies")


def test_a_non_finite_body_is_refused_rather_than_written_unreadable(
    tmp_path: Path,
) -> None:
    """``json.loads`` accepts ``NaN``, so this arrives from a real provider body.

    Writing it would produce a file no strict JSON reader can load back.
    """
    with pytest.raises(ArtifactError, match="could not be rendered as JSON"):
        _write(tmp_path, raw_responses=[{"score": float("nan")}])
    assert not (tmp_path / "research").exists()


def test_a_missing_artifact_names_its_path(tmp_path: Path) -> None:
    """Paths are rendered, uniformly with the rest of the project (M1-401 carve-out).

    An unreadable-artifact error with no path cannot be acted on.
    """
    with pytest.raises(ArtifactError, match="cannot read retrieval artifact") as caught:
        read_raw_responses(tmp_path, "research/42/absent.json")
    assert "absent.json" in str(caught.value)


def test_a_corrupt_artifact_is_refused_without_echoing_its_contents(
    tmp_path: Path,
) -> None:
    relative = artifact_relative_path(question_id=42, retrieval_run_id="run-1")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(f"{{not json at all {PLANTED}", encoding="utf-8")
    with pytest.raises(ArtifactError) as caught:
        read_raw_responses(tmp_path, relative)
    assert PLANTED not in f"{caught.value}{caught.value!r}{caught.value.args}"


def test_an_unknown_schema_version_is_refused_without_echoing_the_found_value(
    tmp_path: Path,
) -> None:
    relative = artifact_relative_path(question_id=42, retrieval_run_id="run-1")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"artifact_schema_version": PLANTED, "raw_responses": []}), encoding="utf-8"
    )
    with pytest.raises(ArtifactError) as caught:
        read_raw_responses(tmp_path, relative)
    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert PLANTED not in rendered
    assert ARTIFACT_SCHEMA_VERSION in rendered


def test_the_envelope_carries_bodies_only_and_no_request_material(
    tmp_path: Path,
) -> None:
    """Response bodies only: a request header or URL would carry the API key.

    Asserted on the written bytes rather than on the API, because the API is not
    where a future change would put one.
    """
    _write(tmp_path)
    envelope = json.loads((tmp_path / "research/42/run-1.json").read_text(encoding="utf-8"))
    assert set(envelope) == {
        "artifact_schema_version",
        "retrieval_run_id",
        "question_id",
        "provider",
        "written_at_utc",
        "raw_responses",
    }


def test_a_naive_timestamp_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="timezone-aware"):
        _write(tmp_path, written_at_utc=datetime(2026, 8, 18, 12, 0))


# --- round-1 review regressions ----------------------------------------------


def test_the_reader_refuses_a_non_finite_constant_the_writer_cannot_emit(
    tmp_path: Path,
) -> None:
    """Finding 7. `json.loads` accepts `NaN`/`Infinity` by default.

    So the reader was accepting bodies the writer refuses to produce, and returned
    them as Python floats as though they had round-tripped. A reader that admits
    more than its writer can emit is not reading the format it documents.
    """
    relative = artifact_relative_path(question_id=42, retrieval_run_id="run-1")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    for constant in ("NaN", "Infinity", "-Infinity"):
        path.write_text(
            '{"artifact_schema_version": "1.0.0", "retrieval_run_id": "run-1", '
            '"question_id": 42, "provider": "asknews", '
            '"written_at_utc": "2026-08-18T12:00:00+00:00", '
            f'"raw_responses": [{{"score": {constant}}}]}}',
            encoding="utf-8",
        )
        with pytest.raises(ArtifactError, match="non-finite JSON constant"):
            read_raw_responses(tmp_path, relative)


def test_the_reader_requires_the_envelopes_provenance_fields(tmp_path: Path) -> None:
    """Finding 7. A version-and-bodies envelope was accepted as a valid artifact.

    With no run id, question, provider or timestamp it is an unattributable blob,
    not a retrieval record — and the whole point of keeping the bytes is saying
    which paid run they came from.
    """
    relative = artifact_relative_path(question_id=42, retrieval_run_id="run-1")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    complete = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "retrieval_run_id": "run-1",
        "question_id": 42,
        "provider": "asknews",
        "written_at_utc": "2026-08-18T12:00:00+00:00",
        "raw_responses": [],
    }
    # The complete envelope reads, so the assertions below are about the missing
    # field and not about some unrelated strictness.
    path.write_text(json.dumps(complete), encoding="utf-8")
    assert read_raw_responses(tmp_path, relative) == ()

    for field in ("retrieval_run_id", "question_id", "provider", "written_at_utc"):
        partial = {k: v for k, v in complete.items() if k != field}
        path.write_text(json.dumps(partial), encoding="utf-8")
        with pytest.raises(ArtifactError, match="missing or malformed"):
            read_raw_responses(tmp_path, relative)


def test_the_reader_refuses_a_malformed_written_at(tmp_path: Path) -> None:
    """Finding 7. `fromisoformat` quotes the offending string, so it is wrapped."""
    relative = artifact_relative_path(question_id=42, retrieval_run_id="run-1")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    for stamp, expected in ((PLANTED, "not an ISO-8601"), ("2026-08-18T12:00:00", "no offset")):
        path.write_text(
            json.dumps(
                {
                    "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                    "retrieval_run_id": "run-1",
                    "question_id": 42,
                    "provider": "asknews",
                    "written_at_utc": stamp,
                    "raw_responses": [],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ArtifactError, match=expected) as caught:
            read_raw_responses(tmp_path, relative)
        assert PLANTED not in f"{caught.value}{caught.value!r}{caught.value.args}"
