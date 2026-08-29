"""T-903: the dry-run acceptance criterion, as a command rather than a harness.

``docs/backlog/backlog.csv`` states it in one sentence -- *"one command produces one
validated record, zero provider calls and zero submission calls"* -- and
``CODEX_HANDOFF.md`` § "Acceptance tests" numbers the same thing as items 1-3, with the
Milestone 1 pass/fail checklist immediately below it. Those are the tests in this file, in
that order; each one names which clause it is discharging.

Every test drives ``main(argv)`` and asserts an exit code, because the criterion is about a
command. The seeded preconditions are ``conftest.py``'s, built through production writers.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.research.packet import packet_sha256

from scenario import (
    QUESTION_ID,
    SEED_ATTEMPT,
    SNAPSHOT,
    Seed,
    config_data,
    seed_scenario,
    write_config,
)


def run_argv(seed: Seed, *extra: str) -> list[str]:
    return [
        "run",
        "--config",
        str(seed.config_file),
        "--question-id",
        str(QUESTION_ID),
        "--snapshot",
        str(SNAPSHOT),
        "--attempt-id",
        SEED_ATTEMPT,
        *extra,
    ]


def printed(out: str) -> dict[str, str]:
    """`run`'s output as a mapping. It prints `label:   value` lines and nothing else."""
    fields: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line and not line.startswith(" "):
            label, _, value = line.partition(":")
            fields[label.strip()] = value.strip()
    return fields


# --- CODEX_HANDOFF acceptance test 1 ------------------------------------------


def test_verify_env_passes_on_the_fixture_config_and_prints_no_secret_value(
    seed: Seed, fake_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """*"verify-env passes with a fixture config and no secrets printed."*

    The env-var **names** are expected in the output -- naming what is missing is the whole
    point of the command -- so what is asserted is the absence of the *values*.
    """
    assert main(["verify-env", "--config", str(seed.config_file)]) == EXIT_OK
    captured = capsys.readouterr()
    for name, value in fake_env.items():
        assert value not in captured.out
        assert value not in captured.err
        assert name in captured.out


# --- CODEX_HANDOFF acceptance test 2 ------------------------------------------


def test_run_produces_exactly_one_validated_record(
    seed: Seed, ledger: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """*"`run --question-id <fixture> --dry-run --no-submit` creates exactly one complete
    validated forecast record."*

    "Exactly one" is the load-bearing word and the reason ``conftest`` refuses to seed
    through ``persist_generation``: with a record already present, this count could not
    fail. The ledger holds research and an artifact when this starts, and no forecast row.
    """
    assert ledger.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 0

    assert main(run_argv(seed, "--dry-run", "--no-submit")) == EXIT_OK
    fields = printed(capsys.readouterr().out)

    rows = ledger.execute(
        "SELECT record_id, question_id, tournament_id, forecast_version, attempt_id "
        "FROM forecast_records"
    ).fetchall()
    assert len(rows) == 1
    record_id, question_id, tournament_id, version, attempt_id = rows[0]
    assert (question_id, tournament_id, version) == (QUESTION_ID, "minibench", 1)
    assert fields["record"] == record_id
    assert fields["status"] == "validated"

    # The record is stamped with a freshly minted attempt id, not the one replayed. That is
    # `run_replay`'s deliberate choice -- `idx_forecast_records_attempt_id` is partial-unique
    # and `004`'s trigger cross-checks `pipeline_failure_events`, so reusing the saved id
    # would collide the moment that attempt also has a record.
    assert attempt_id != SEED_ATTEMPT
    assert f"replayed from {SEED_ATTEMPT}" in fields["attempt"]

    validated = ledger.execute(
        "SELECT count(*) FROM lifecycle_events "
        "WHERE forecast_record_id = ? AND event_type = 'validated'",
        (record_id,),
    ).fetchone()[0]
    assert validated == 1


def test_run_writes_the_complete_milestone_one_record(
    seed: Seed, ledger: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """``CODEX_HANDOFF.md`` § "Objective Milestone 1 pass/fail", clause by clause.

    The criterion says *complete* validated record, and "complete" is defined there rather
    than in the backlog row: question/tournament/model/prompt/retrieval metadata, at least
    one source, and a forecast carrying base rate, prior, evidence adjustments, failure
    modes and the typed prediction.
    """
    assert main(run_argv(seed, "--dry-run", "--no-submit")) == EXIT_OK
    fields = printed(capsys.readouterr().out)

    ledger.row_factory = sqlite3.Row
    row = ledger.execute("SELECT * FROM forecast_records").fetchone()

    # Complete question/tournament/model/prompt/retrieval metadata, as columns.
    for column in (
        "question_id",
        "question_type",
        "tournament_id",
        "model_provider",
        "model_name",
        "prompt_version",
        "prompt_sha256",
        "retrieval_run_id",
        "generated_at_utc",
        "forecast_sha256",
        "raw_output_path",
    ):
        assert row[column] is not None, column

    # The prompt hash is the file's, not a value carried over from anywhere else (M1-401).
    assert row["prompt_sha256"] == seed.settings.prompt_sha256
    assert (row["model_provider"], row["model_name"]) == (
        seed.config.model.provider,
        seed.config.model.name,
    )
    assert row["forecast_sha256"] == fields["hash"]

    record = json.loads(row["record_json"])

    # Replayable research: the packet hash *inside the record* -- and therefore inside
    # `forecast_sha256` -- is the hash of the packet that was actually replayed, which is
    # what makes the attribution claim checkable later. It is a record field rather than a
    # column, which is the point: a column could be edited without changing the hash.
    assert record["research_packet_sha256"] == packet_sha256(seed.packet)
    assert fields["packet"] == record["research_packet_sha256"]

    # At least one source, resolved to the documents the model was shown. (The
    # `insufficient_research` alternative is a different scenario; this one has research, so
    # the source count must be real.)
    assert len(record["sources"]) == 2
    assert [source["source_id"] for source in record["sources"]] == ["src-001", "src-002"]
    assert fields["research"].endswith("2 source(s)")

    # Base rate, prior, evidence adjustments, failure modes and the typed forecast.
    forecast = record["forecast"]
    assert forecast["base_rate"]["prior_probability"] is not None
    assert forecast["model_prior"] is not None
    assert forecast["evidence_adjustments"]
    assert forecast["failure_modes"]
    assert forecast["final_prediction"]["probability_yes"] is not None

    # Community prediction is never a forecaster input in v1 (a hard constraint). The
    # record carries the claim explicitly rather than by omission, so assert the claim: no
    # snapshot was taken and it was not used as input.
    assert record["community_prediction"]["used_as_model_input"] is False
    assert record["community_prediction"]["snapshot"] is None

    # No submission network call: the command says so, and nothing here can make one.
    assert fields["submitted"].startswith("no")


# --- CODEX_HANDOFF acceptance test 3 ------------------------------------------


def test_replaying_the_record_reproduces_its_forecast_hash(
    seed: Seed, capsys: pytest.CaptureFixture[str]
) -> None:
    """*"Replaying saved research and saved model output ... reproduces the same forecast
    hash."*

    The two halves of the criterion are two commands: ``run`` writes the record and prints
    its hash, and ``replay --record-id`` re-derives that hash from the saved artifact alone.
    This is the end-to-end proof, and the reason ``run`` prints the hash at all -- an
    operator's next command is ``approve --forecast-sha256 <hash>``.
    """
    assert main(run_argv(seed, "--dry-run", "--no-submit")) == EXIT_OK
    written = printed(capsys.readouterr().out)

    assert (
        main(["replay", "--config", str(seed.config_file), "--record-id", written["record"]])
        == EXIT_OK
    )
    replayed = printed(capsys.readouterr().out)
    assert replayed["verdict"] == "match"
    assert replayed["stored"] == replayed["replayed"] == written["hash"]


# --- the two zeroes, structurally ---------------------------------------------

# `whiskeyjack_bot` modules that can spend money or post. None may be reachable by importing
# the pipeline: if none is on the graph there is no code here to make a call with, and none
# can be added without this failing. The check is deliberately about *our* adapters and not
# about third-party packages -- see the module docstring of `whiskeyjack_bot.pipeline` on why
# a forbidden-package set copied from `test_research_store.py` would be either wrong or
# vacuous here: `metaculus/snapshots.py` and `questions/normalize.py` both import
# `forecasting_tools` to parse a saved snapshot, and it drags every HTTP client with it.
PAID_OR_POSTING = (
    "whiskeyjack_bot.research.asknews",
    "whiskeyjack_bot.research.exa",
    "whiskeyjack_bot.forecast.generate",
    "whiskeyjack_bot.metaculus.client",
    "whiskeyjack_bot.metaculus.fetch",
    "whiskeyjack_bot.metaculus.poster",
    "whiskeyjack_bot.submission",
    "whiskeyjack_bot.submission.gateway",
    "whiskeyjack_bot.submission.live",
    "whiskeyjack_bot.approval",
)


def imported_by(statements: str) -> set[str]:
    """The ``sys.modules`` delta of some imports, measured in a clean interpreter.

    A subprocess because in-process ``sys.modules`` is polluted by every other test that
    imported an adapter -- checking it here would assert nothing. The shape is
    ``tests/unit/test_research_store.py``'s.
    """
    program = (
        "import sys;before=set(sys.modules);"
        f"{statements}"
        "print(','.join(sorted(set(sys.modules)-before)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    return {name for name in result.stdout.strip().split(",") if name}


def test_the_pipeline_cannot_reach_a_paid_or_posting_module() -> None:
    """Zero provider calls and zero submission calls, as a property of the import graph.

    Both zeroes at once, because the same delta answers both and splitting it into two
    subprocess launches would double the cost to say the same thing.
    """
    added = imported_by("import whiskeyjack_bot.pipeline;")
    assert not (added & set(PAID_OR_POSTING)), sorted(added & set(PAID_OR_POSTING))
    assert not {name for name in added if name.startswith("whiskeyjack_bot.submission")}


def test_the_guard_is_not_vacuous() -> None:
    """The same measurement, against a module that *does* reach a paid client.

    Without this the test above passes whether or not ``imported_by`` measures anything --
    a typo'd program string, an empty delta, a module name that never resolves would all
    read as success. ``forecast.generate`` is the live model call, so it must trip the guard
    the pipeline clears. This is ``docs/LESSONS.md`` § 5 applied to a non-property test:
    check that the check can fail.
    """
    added = imported_by("import whiskeyjack_bot.forecast.generate;")
    assert "whiskeyjack_bot.forecast.generate" in added & set(PAID_OR_POSTING)


def pipeline_import_targets() -> set[str]:
    """Every ``whiskeyjack_bot`` module named by an import statement in ``pipeline.py``.

    An AST walk rather than a text scan, and over the whole tree rather than the module
    header, because the thing being looked for is precisely an import that is *not* in the
    header: a function-local one. ``ast.walk`` reaches a statement inside a function body,
    inside ``if TYPE_CHECKING``, inside anything.
    """
    import ast
    import inspect

    import whiskeyjack_bot.pipeline as pipeline

    named: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(pipeline))):
        if isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            named.add(node.module)
    return {name for name in named if name.startswith("whiskeyjack_bot")}


def test_the_command_path_imports_no_more_than_the_module_does() -> None:
    """The hole a module-level graph test leaves open, closed by measurement.

    A deferred, function-local import is invisible to the test above: the module-level graph
    looks clean while the executed path pulls a client in. ``_select_question`` used to do
    exactly that, importing ``metaculus.fetch`` -- which is ``load_snapshot`` plus a log line
    but sits beside the live fetcher and so imports ``metaculus.client``. The graph test
    passed the whole time. So this asserts the stronger thing: importing *everything*
    ``pipeline.py`` names, from anywhere in its source, still reaches no paid or posting
    module, and adds nothing to what importing the module alone added.
    """
    targets = pipeline_import_targets()
    # Anti-vacuity: an AST walk that found nothing would pass every assertion below.
    assert "whiskeyjack_bot.forecast.replay" in targets
    assert "whiskeyjack_bot.metaculus.snapshots" in targets
    assert len(targets) > 10

    exercised = imported_by("".join(f"import {name};" for name in sorted(targets)))
    assert not exercised & set(PAID_OR_POSTING), sorted(exercised & set(PAID_OR_POSTING))
    assert not (exercised - imported_by("import whiskeyjack_bot.pipeline;"))


# --- the flags are assertions, not overrides ----------------------------------


def test_dry_run_flag_refuses_a_config_that_contradicts_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` asserts ``submission.dry_run``; it does not force it.

    A flag that silently forced the safe value would let a config with ``dry_run: false``
    pass a command line that reads as safe, and the operator would have been told the wrong
    thing about their own file. Nothing is written when it refuses.
    """
    data = config_data(tmp_path)
    data["submission"]["dry_run"] = False
    seeded = seed_scenario(write_config(tmp_path, data))

    assert main(run_argv(seeded, "--dry-run")) == EXIT_REFUSED
    assert "submission.dry_run is not set" in capsys.readouterr().out

    conn = sqlite3.connect(seeded.config.storage.sqlite_path)
    try:
        assert conn.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 0
    finally:
        conn.close()


def test_no_submit_flag_refuses_a_config_that_contradicts_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = config_data(tmp_path)
    data["submission"]["no_submit"] = False
    seeded = seed_scenario(write_config(tmp_path, data))

    assert main(run_argv(seeded, "--no-submit")) == EXIT_REFUSED
    assert "submission.no_submit is not set" in capsys.readouterr().out


def test_the_committed_defaults_already_satisfy_both_flags(seed: Seed) -> None:
    """The other direction, and the reason omitting the flags asserts nothing.

    ``config.example.yaml`` commits ``dry_run: true`` and ``no_submit: true``, so the safe
    command line and the safe config agree out of the box. If this ever fails, the committed
    defaults changed and the hard constraint in ``CLAUDE.md`` was broken somewhere else.
    """
    assert seed.config.submission.dry_run is True
    assert seed.config.submission.no_submit is True
    assert seed.config.submission.enabled is False
    assert main(run_argv(seed, "--dry-run", "--no-submit")) == EXIT_OK
