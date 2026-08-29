"""What `run` refuses, and what it leaves behind when it does (T-903).

Lives beside the acceptance criteria rather than in ``tests/unit`` because it needs the same
seeded scenario, and because the claim it checks is one of the criterion's own: a command
whose whole point is that it writes exactly one validated record has to write **zero** when
it refuses. ``run_replay``'s docstring makes that claim explicitly -- every refusal except
one writes nothing, because none of the others is a fact about a forecast attempt and
recording one would put a failure in the ledger that never happened.

So every test here asserts two things: that the module's own error type arrives (never a
raw ``AttributeError``/``KeyError``/``ValueError``, which is a review finding in this
project and has been twice), and that the three writable tables are untouched.

The one exception is at the bottom: a reply that parses and fails validation *is* a fact
about an attempt, and is recorded as a ``generation_failed`` pipeline event.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from scenario import (
    NOW,
    QUESTION_ID,
    SEED_ATTEMPT,
    SNAPSHOT,
    Seed,
    config_data,
    model_settings,
    research_run,
    seed_scenario,
    write_config,
)
from whiskeyjack_bot.config import load_config
from whiskeyjack_bot.forecast.parse import ModelSettings
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.pipeline import ForecastRejected, PipelineError, run_replay
from whiskeyjack_bot.research.store import open_run

TABLES = ("forecast_records", "lifecycle_events", "pipeline_failure_events")


def counts(config_path: Path) -> dict[str, int]:
    conn = connect(load_config(config_path).storage.sqlite_path)
    try:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        conn.close()


def attempt_run(
    seed: Seed,
    *,
    config_path: Path | None = None,
    question_id: int = QUESTION_ID,
    attempt_id: str | None = None,
    snapshot: Path = SNAPSHOT,
    now: datetime = NOW,
) -> None:
    """Run once against the seeded scenario. Raises whatever the pipeline raises."""
    path = config_path or seed.config_file
    config = load_config(path)
    conn = connect(config.storage.sqlite_path)
    try:
        run_replay(
            conn,
            config,
            question_id=question_id,
            attempt_id=seed.attempt_id if attempt_id is None else attempt_id,
            snapshot=snapshot,
            now=now,
        )
    finally:
        conn.close()


def refused(seed: Seed, match: str, **kwargs: Any) -> None:
    """Assert the run raises ``PipelineError`` matching ``match`` and writes nothing."""
    path = kwargs.get("config_path") or seed.config_file
    before = counts(path)
    with pytest.raises(PipelineError, match=match):
        attempt_run(seed, **kwargs)
    assert counts(path) == before == dict.fromkeys(TABLES, 0)


# --- the two replay switches --------------------------------------------------


@pytest.mark.parametrize(
    ("section", "key", "message"),
    [
        ("retrieval", "replay_saved_research", "replay_saved_research is disabled"),
        ("forecast", "replay_saved_model_output", "replay_saved_model_output is disabled"),
    ],
)
def test_a_disabled_replay_switch_refuses_before_anything_is_read(
    tmp_path: Path, section: str, key: str, message: str
) -> None:
    """Both are committed ``false``, and both are checked together, up front.

    Checked as a pair rather than one at a time at the point of use: a caller that reached
    the second gate having silently satisfied only the first would have loaded a snapshot and
    opened a ledger for a run that was never going to be permitted.
    """
    seed = seed_scenario(write_config(tmp_path, config_data(tmp_path)))
    data = config_data(tmp_path)
    data[section][key] = False
    refused(seed, message, config_path=write_config(tmp_path, data, name="off.yaml"))


# --- the question, and the research --------------------------------------------


def test_a_question_the_snapshot_does_not_hold_is_refused(seed: Seed) -> None:
    refused(seed, "no supported question with that id", question_id=90909)


def test_the_message_does_not_render_the_question_id(seed: Seed) -> None:
    """A question id is row content (M1-202's precedent), so it is withheld like any value."""
    with pytest.raises(PipelineError) as caught:
        attempt_run(seed, question_id=90909)
    assert "90909" not in str(caught.value)


def test_a_question_with_no_completed_research_is_refused(tmp_path: Path) -> None:
    """An *open* run is not research: it is a spend recorded before its result came back.

    Seeded here by persisting the run with ``completed_at_utc=None``, which is the state
    ``open_run`` leaves behind when a provider call is in flight or died mid-call.
    """
    seed = seed_scenario(write_config(tmp_path, config_data(tmp_path)))

    # A second ledger over the same artifact root, holding only the open run. The seeded
    # rows cannot be removed from the first one and should not be: `003` makes
    # research_documents append-only, and "retrieved evidence is never deleted" is the
    # property this project exists to keep -- a test that worked around it would be
    # asserting against a ledger the product cannot produce.
    data = config_data(tmp_path)
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bare.sqlite3")
    bare = write_config(tmp_path, data, name="bare.yaml")
    config = load_config(bare)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    try:
        # `open_run`, not `persist_retrieval`: the latter requires a completed run, which
        # is the same rule stated one layer down.
        open_run(conn, research_run(completed_at_utc=None))
    finally:
        conn.close()

    refused(seed, "no completed research run for that question", config_path=bare)


# --- the saved reply ------------------------------------------------------------


def test_an_artifact_that_is_not_there_is_refused(seed: Seed) -> None:
    """The path *is* rendered, and deliberately: it is the settled M1-401 carve-out, and a
    "cannot read raw model output artifact" with no path cannot be acted on."""
    refused(seed, "cannot read raw model output artifact", attempt_id="never-written")

    with pytest.raises(PipelineError) as caught:
        attempt_run(seed, attempt_id="never-written")
    assert "never-written.json" in str(caught.value)


def test_an_artifact_stored_under_another_attempt_is_refused(tmp_path: Path) -> None:
    """A copied or hand-moved artifact, which is the worst thing this could get wrong.

    The path is *derived* from ``(question_id, attempt_id)``, so an envelope disagreeing with
    the name it was found under means the file is not the one the layout says it is.
    Replaying it would attribute one attempt's reply to another.
    """
    config_file = write_config(tmp_path, config_data(tmp_path))
    seed = seed_scenario(config_file, attempt_id="the-real-attempt")
    impostor = seed.artifact.parent / f"{SEED_ATTEMPT}.json"
    impostor.write_bytes(seed.artifact.read_bytes())
    # Addressed by the *impostor's* name, which is the whole point: the file is found, and
    # the envelope inside it names a different attempt.
    refused(seed, "records a different attempt or question", attempt_id=SEED_ATTEMPT)


def test_an_artifact_recording_a_failed_attempt_is_refused(tmp_path: Path) -> None:
    """Nothing to replay is not the same finding as a replay that did not match."""
    config_file = write_config(tmp_path, config_data(tmp_path))
    seed = seed_scenario(config_file)
    envelope = json.loads(seed.artifact.read_text(encoding="utf-8"))
    envelope["failure_code"] = "schema_invalid"
    envelope["failure_problems"] = ["final_prediction: field required"]
    seed.artifact.write_text(json.dumps(envelope), encoding="utf-8")
    refused(seed, "recorded a failure and produced no forecast")


def test_an_artifact_carrying_no_reply_is_refused(tmp_path: Path) -> None:
    config_file = write_config(tmp_path, config_data(tmp_path))
    seed = seed_scenario(config_file)
    envelope = json.loads(seed.artifact.read_text(encoding="utf-8"))
    envelope["raw_responses"] = []
    seed.artifact.write_text(json.dumps(envelope), encoding="utf-8")
    refused(seed, "carries no model reply")


# --- the model and the prompt the reply was produced under ------------------------


def test_a_reply_from_another_model_is_not_attributed_to_this_one(tmp_path: Path) -> None:
    """``model_provider``/``model_name`` become NOT NULL columns: they are the claim that a
    particular model produced this forecast, and nothing in the ledger could later detect a
    run that persisted them from an artifact the configuration contradicts."""
    config_file = write_config(tmp_path, config_data(tmp_path))
    config = load_config(config_file)
    settings = model_settings(config)
    seed = seed_scenario(
        config_file,
        settings=ModelSettings(
            **{**settings.__dict__, "name": "openrouter/some-other-model"},
        ),
    )
    refused(seed, "produced by a different model")


def test_a_reply_from_another_prompt_version_is_refused(tmp_path: Path) -> None:
    config_file = write_config(tmp_path, config_data(tmp_path))
    config = load_config(config_file)
    settings = model_settings(config)
    seed = seed_scenario(
        config_file,
        settings=ModelSettings(**{**settings.__dict__, "prompt_version": "0.9.0"}),
    )
    refused(seed, "different forecaster prompt version")


def test_an_edited_prompt_whose_version_did_not_change_is_refused(tmp_path: Path) -> None:
    """The check M1-401 exists for: the version can stay ``1.1.0`` while the bytes change.

    The prompt is copied into ``tmp_path`` and edited there rather than in the repository,
    so the config points at a file this test owns. ``prompt_sha256`` is recomputed from disk
    on every run, which is what makes the edit visible at all.
    """
    data = config_data(tmp_path)
    local_prompt = tmp_path / "forecaster.md"
    original = Path(data["forecast"]["prompt_path"]).read_text(encoding="utf-8")
    local_prompt.write_text(original, encoding="utf-8")
    data["forecast"]["prompt_path"] = str(local_prompt)
    config_file = write_config(tmp_path, data)
    seed = seed_scenario(config_file)

    local_prompt.write_text(original + "\n<!-- one more byte, same version -->\n", encoding="utf-8")
    refused(seed, "its version is unchanged but its bytes are not")


# --- the load-bearing check: the reply answered *this* research ---------------------


def test_a_reply_that_answered_different_research_is_refused(tmp_path: Path) -> None:
    """The module's central refusal. A saved reply is an answer to a specific packet;
    replaying it against research it never saw would write an attribution claim the reply
    does not support -- documents cited in a record the model was never shown."""
    config_file = write_config(tmp_path, config_data(tmp_path))
    seed = seed_scenario(config_file)
    other = json.loads(seed.request)
    other["question_text"] = "a question the model was never actually asked"
    # A distinct attempt id: the artifact writer never overwrites, which is M2-709's rule
    # and the reason a second seed over one ledger needs its own name.
    seed_again = seed_scenario(config_file, request=json.dumps(other), attempt_id="answered-other")
    refused(seed_again, "does not match the one the saved reply answered")


def test_the_mismatch_message_renders_neither_packet(tmp_path: Path) -> None:
    """Both packets carry the question text and every document body, so neither may appear."""
    config_file = write_config(tmp_path, config_data(tmp_path))
    marker = "a question the model was never actually asked"
    other = json.loads(seed_scenario(config_file).request)
    other["question_text"] = marker
    seed = seed_scenario(config_file, request=json.dumps(other), attempt_id="answered-other")
    with pytest.raises(PipelineError) as caught:
        attempt_run(seed)
    assert marker not in str(caught.value)
    assert "example.org" not in str(caught.value)


def test_a_stored_request_that_is_not_json_is_refused(tmp_path: Path) -> None:
    """``_as_of_from_request`` reads the stored request to recover the one value that cannot
    be re-derived. Its failures are the module's error type like everything else."""
    config_file = write_config(tmp_path, config_data(tmp_path))
    seed = seed_scenario(config_file, request="not json at all")
    refused(seed, "stored request is not valid JSON")


# --- the one failure that IS recorded ------------------------------------------------


def test_a_reply_that_fails_validation_records_the_failure_and_no_record(
    tmp_path: Path,
) -> None:
    """``generation_failed`` (M1-606), because the pipeline attempted something and it did
    not work -- unlike every refusal above, which is not a fact about a forecast attempt.

    The record is still not written, which is the half that matters: a rejected reply must
    not leave a forecast behind, and a failure must not be silent.
    """
    config_file = write_config(tmp_path, config_data(tmp_path))
    seed = seed_scenario(config_file)

    # The reply is broken *in the artifact*, not in the seed: `build_generation` validates
    # what it is given, exactly as the generating call does, so an invalid payload cannot be
    # seeded through it. Rewriting the stored raw text is the honest reproduction anyway --
    # a reply that fails validation is text on disk that no longer parses, which is the
    # state a bad model call leaves behind. A probability above 1 parses as JSON and fails
    # the schema, which is what `_classify` turns into `schema_invalid` rather than
    # `malformed_response`; asserting the code pins which of the two happened.
    envelope = json.loads(seed.artifact.read_text(encoding="utf-8"))
    reply = json.loads(envelope["raw_responses"][0])
    reply["final_prediction"]["probability_yes"] = 1.7
    envelope["raw_responses"] = [json.dumps(reply)]
    seed.artifact.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ForecastRejected) as caught:
        attempt_run(seed)
    assert caught.value.problems
    assert caught.value.attempt_id != SEED_ATTEMPT

    conn = connect(seed.config.storage.sqlite_path)
    try:
        assert conn.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 0
        rows = conn.execute(
            "SELECT event_type, detail_code, question_id, attempt_id FROM pipeline_failure_events"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    event_type, detail_code, question_id, attempt_id = rows[0]
    assert (event_type, detail_code, question_id) == (
        "generation_failed",
        "schema_invalid",
        QUESTION_ID,
    )
    assert attempt_id == caught.value.attempt_id


# --- the freshly minted attempt id is not an accident ---------------------------------


def test_two_runs_of_the_same_seed_write_two_records_with_different_hashes(
    tmp_path: Path,
) -> None:
    """Pins the consequence of minting, so nobody 'fixes' the hash by reusing the saved id.

    Reusing ``--attempt-id`` on the record would make two runs hash identically, which reads
    like an improvement -- until the second one collides on
    ``idx_forecast_records_attempt_id``, a partial unique index, or trips ``004``'s trigger
    against ``pipeline_failure_events``. The reproducible hash the criterion asks for is
    ``replay --record-id`` re-deriving *one* record's hash, which
    ``test_dry_run_acceptance`` asserts; it is not two runs agreeing.
    """
    config_file = write_config(tmp_path, config_data(tmp_path))
    seed = seed_scenario(config_file)
    attempt_run(seed)
    attempt_run(seed, now=NOW.replace(hour=16))

    conn = connect(seed.config.storage.sqlite_path)
    try:
        rows = conn.execute(
            "SELECT attempt_id, forecast_sha256, forecast_version FROM forecast_records "
            "ORDER BY forecast_version"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert [row[2] for row in rows] == [1, 2]
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] != rows[1][1]


# --- config shapes the module refuses outright -----------------------------------------


@pytest.mark.parametrize("bad_now", [datetime(2026, 8, 21, 15, 30), "not a datetime", None])
def test_a_naive_or_absent_now_is_refused(seed: Seed, bad_now: object) -> None:
    """``now`` is caller-supplied so it can be the pipeline's record of when the run
    happened rather than the model's claim about the world -- which makes validating it this
    module's job."""
    config = load_config(seed.config_file)
    conn = connect(config.storage.sqlite_path)
    try:
        with pytest.raises(PipelineError, match="timezone-aware"):
            run_replay(
                conn,
                config,
                question_id=QUESTION_ID,
                attempt_id=SEED_ATTEMPT,
                snapshot=SNAPSHOT,
                now=bad_now,  # type: ignore[arg-type]
            )
    finally:
        conn.close()


def test_a_config_that_is_not_an_app_config_is_refused(seed: Seed) -> None:
    """Arrives as ``PipelineError``, not as the ``AttributeError`` a duck-typed read would
    raise. A raw exception escaping a public boundary is a finding in this project."""
    conn = connect(load_config(seed.config_file).storage.sqlite_path)
    try:
        with pytest.raises(PipelineError, match="must be an AppConfig"):
            run_replay(
                conn,
                object(),  # type: ignore[arg-type]
                question_id=QUESTION_ID,
                attempt_id=SEED_ATTEMPT,
                snapshot=SNAPSHOT,
                now=datetime(2026, 8, 21, 15, 30, tzinfo=timezone.utc),
            )
    finally:
        conn.close()


def test_a_snapshot_that_is_not_a_path_is_refused(seed: Seed) -> None:
    config = load_config(seed.config_file)
    conn = connect(config.storage.sqlite_path)
    try:
        with pytest.raises(PipelineError, match="snapshot must be a Path"):
            run_replay(
                conn,
                config,
                question_id=QUESTION_ID,
                attempt_id=SEED_ATTEMPT,
                snapshot=str(SNAPSHOT),  # type: ignore[arg-type]
                now=NOW,
            )
    finally:
        conn.close()


def test_an_unreadable_snapshot_arrives_as_the_modules_error(seed: Seed, tmp_path: Path) -> None:
    """A ``SnapshotError`` is translated, and the path it names survives -- the settled
    M1-401 carve-out is what makes an unreadable snapshot actionable."""
    empty = tmp_path / "not-a-snapshot.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(PipelineError) as caught:
        attempt_run(seed, snapshot=empty)
    assert str(empty) in str(caught.value)


def test_yaml_config_still_loads_after_every_edit_in_this_file(tmp_path: Path) -> None:
    """Guards the fixture, not the product.

    Every refusal above is built by editing ``config_data`` and writing it back, and a test
    that silently produced an *invalid* config would refuse for the wrong reason while still
    passing. So: the unmodified data really does load.
    """
    path = write_config(tmp_path, config_data(tmp_path))
    assert load_config(path).forecast.replay_saved_model_output is True
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["retrieval"]["replay_saved_research"]
