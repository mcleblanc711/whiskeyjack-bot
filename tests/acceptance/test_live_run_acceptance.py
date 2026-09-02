"""M1-315 acceptance: one command, live retrieval and a live model call, one record each.

The criterion, in full: *"One command forecasts one or more snapshot questions through live
retrieval and a live model call, writing one validated record per question; a per-question
failure records its pipeline event and does not abort the batch; retrying a later phase
repeats no paid call unless asked; the replay path's structural zero-provider-call guarantee
is unchanged, which requires the paid composition to live outside ``whiskeyjack_bot.pipeline``
or that module's import-graph test to be restated."*

The last clause is the one this file spends the most on, because it is the clause that can be
satisfied dishonestly. It is met by **living outside** ``pipeline.py``: that module is not
edited, its three guards in ``test_dry_run_acceptance.py`` are untouched and still green, and
the tests below add the corresponding claim for the new module -- which is deliberately a
*different* claim, because a module that spends money cannot assert the same zero.

What ``pipeline_live`` may reach: the paid adapters, necessarily. What it may not: any
submission or approval module. One honest caveat is asserted rather than hidden --
``metaculus.client`` **is** on the graph, because ``research/exa.py`` imports
``MissingCredentialError`` from the module that also holds ``build_poster``. That coupling
predates this branch and is filed as its own row; the guard names it instead of pretending it
is not there, because a guard that quietly excludes what it cannot prove is the vacuity this
project keeps paying for.

Both provider clients are recording doubles installed at ``pipeline_live``'s own construction
seam, and the suite runs under three network guards -- so a double that failed to install
would fail the run rather than pass it quietly.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from asknews_sdk.dto.base import Author, Entities
from asknews_sdk.dto.news import SearchResponse, SearchResponseDictItem
from pydantic import AnyUrl
from scenario import PROMPT_PATH, REPO_ROOT, SNAPSHOT, TOURNAMENT, open_ledger

from whiskeyjack_bot.cli import main
from whiskeyjack_bot.config import load_config
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.ledger import connect

EXIT_REFUSED = 4
BINARY, MULTIPLE_CHOICE, NUMERIC = 91001, 91003, 91002
MODEL_NAME = "openrouter/test-model"

# The whiskeyjack modules a *paid* run must still not be able to reach. Narrower than
# `test_dry_run_acceptance.py`'s set on purpose: this module spends money by design, so
# forbidding the paid adapters would forbid the feature. What stays forbidden is posting.
POSTING = (
    "whiskeyjack_bot.submission",
    "whiskeyjack_bot.submission_gateway",
    "whiskeyjack_bot.submission_live",
    "whiskeyjack_bot.approval",
)
# What it must reach, so the guard above cannot pass by measuring nothing.
PAID = (
    "whiskeyjack_bot.forecast.generate",
    "whiskeyjack_bot.research.asknews",
    "whiskeyjack_bot.research.exa",
)


# --- the live scenario ------------------------------------------------------------------


def live_config_data(tmp_path: Path) -> dict[str, Any]:
    """The example config with writable paths moved, and **both replay switches off**.

    ``scenario.config_data`` turns them on, which is right for the replay command and is
    exactly what the live one refuses -- so this cannot reuse it. Three questions are allowed
    because the committed ``max_questions: 1`` is the safe default and raising it is the
    deliberate operator act the batch loop exists to bound.
    """
    data: dict[str, Any] = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = MODEL_NAME
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(PROMPT_PATH)
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    data["run_limits"]["max_questions"] = 3
    return data


@pytest.fixture()
def live_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(live_config_data(tmp_path)), encoding="utf-8")
    open_ledger(load_config(path)).close()
    return path


def _article(url: str) -> SearchResponseDictItem:
    return SearchResponseDictItem.model_construct(
        article_url=AnyUrl(url),
        article_id=uuid.uuid5(uuid.NAMESPACE_URL, url),
        classification=["Business"],
        country="US",
        source_id="Example Wire",
        page_rank=3,
        domain_url="example.org",
        eng_title="Example headline",
        entities=Entities(),
        keywords=["example"],
        language="en",
        pub_date=datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc),
        summary="An example summary.",
        title="Example headline",
        sentiment=0,
        as_string_key="k1",
        crawl_date=datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc),
        full_text="Example body text.",
        authors=[Author(name="A. Reporter", email=None, url=None)],
    )


class _NewsAPI:
    def __init__(self, fail_for: set[str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_for = fail_for

    def search_news(self, **kwargs: Any) -> SearchResponse:
        self.calls.append(kwargs)
        if any(marker in kwargs.get("query", "") for marker in self._fail_for):
            raise RuntimeError("upstream said no")
        return SearchResponse.model_construct(
            as_dicts=[_article("https://example.org/a"), _article("https://example.org/b")]
        )


class _SDK:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.news = _NewsAPI(fail_for or set())


class _Forecaster:
    def __init__(self, replies: dict[int, str]) -> None:
        self.model = MODEL_NAME
        self._replies = replies
        self.calls: list[int] = []

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        request = json.loads(next(m for m in prompt if m["role"] == "user")["content"])
        question_id = int(request["question_id"])
        self.calls.append(question_id)
        return self._replies.get(question_id, "not json")


@pytest.fixture()
def doubles(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install recording doubles at ``pipeline_live``'s own construction seam.

    The builders are what read credentials and open transports, so replacing exactly them
    leaves every line of composition, validation and persistence under test running the code
    that runs in production. The returned handles are asserted on: a double nobody called is
    a test that measured nothing.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
    from test_pipeline_live import reply_for  # the prompt-derived reply builder

    from whiskeyjack_bot.metaculus.snapshots import load_snapshot
    from whiskeyjack_bot.questions.normalize import normalize_questions

    _, loaded = load_snapshot(SNAPSHOT)
    replies = {q.question_id: reply_for(q) for q in normalize_questions(loaded).questions}

    handles: dict[str, Any] = {"sdk": _SDK(), "forecaster": _Forecaster(replies)}
    monkeypatch.setattr(
        "whiskeyjack_bot.pipeline_live.build_asknews_client", lambda config: handles["sdk"]
    )
    monkeypatch.setattr(
        "whiskeyjack_bot.pipeline_live.build_forecaster_client",
        lambda config: handles["forecaster"],
    )
    monkeypatch.setattr("whiskeyjack_bot.pipeline_live.build_exa_client", lambda config: object())
    return handles


def run_argv(config: Path, *extra: str) -> list[str]:
    return ["run", "--config", str(config), "--snapshot", str(SNAPSHOT), *extra]


def printed(out: str) -> list[dict[str, str]]:
    """The command's output as one mapping per question, plus a final summary mapping."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip() or ":" not in line:
            continue
        label, _, value = line.partition(":")
        if label.strip() == "question" and current:
            blocks.append(current)
            current = {}
        current[label.strip()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def ledger_rows(config: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = connect(load_config(config).storage.sqlite_path)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


# --- the criterion ------------------------------------------------------------------------


def test_one_command_writes_one_validated_record_per_question(
    live_config: Path, doubles: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ledger_rows(live_config, "SELECT record_id FROM forecast_records") == []

    assert main(run_argv(live_config, "--limit", "3", "--dry-run", "--no-submit")) == EXIT_OK
    out = capsys.readouterr().out

    assert len(doubles["forecaster"].calls) == 3, "the model double was not exercised"
    assert doubles["sdk"].news.calls, "the retrieval double was not exercised"
    records = ledger_rows(
        live_config, "SELECT question_id, tournament_id, status FROM forecast_records"
    )
    assert sorted(row[0] for row in records) == sorted([BINARY, MULTIPLE_CHOICE, NUMERIC])
    assert {row[1] for row in records} == {TOURNAMENT}
    validated = ledger_rows(
        live_config, "SELECT 1 FROM lifecycle_events WHERE event_type = 'validated'"
    )
    assert len(validated) == 3
    assert ledger_rows(live_config, "SELECT 1 FROM pipeline_failure_events") == []

    blocks = printed(out)
    summary = blocks[-1]
    assert summary["records"] == "3 of 3 question(s)"
    assert summary["stopped"] == "completed"
    assert summary["submitted"].startswith("no")
    # Two spend figures, never one: `cost_usd is None` means unknown, never free.
    assert "unpriced call(s)" in summary["spend"]


def test_a_per_question_failure_is_recorded_and_the_batch_continues(
    live_config: Path, doubles: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The isolation clause. The middle question's provider fails; the other two record."""
    from whiskeyjack_bot.metaculus.snapshots import load_snapshot
    from whiskeyjack_bot.questions.normalize import normalize_questions

    _, loaded = load_snapshot(SNAPSHOT)
    doomed = next(
        q.title for q in normalize_questions(loaded).questions if q.question_id == MULTIPLE_CHOICE
    )
    doubles["sdk"] = _SDK(fail_for={doomed})

    assert main(run_argv(live_config, "--limit", "3")) == EXIT_REFUSED
    capsys.readouterr()

    recorded = ledger_rows(live_config, "SELECT question_id FROM forecast_records")
    assert sorted(row[0] for row in recorded) == sorted([BINARY, NUMERIC])
    events = ledger_rows(
        live_config, "SELECT event_type, detail_code, question_id FROM pipeline_failure_events"
    )
    assert events == [("research_failed", "provider_error", MULTIPLE_CHOICE)]


def test_a_rerun_repeats_no_paid_retrieval_call(
    live_config: Path, doubles: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """*Retrying a later phase repeats no paid call unless asked*, by call count."""
    assert main(run_argv(live_config, "--question-id", str(BINARY))) == EXIT_OK
    first = len(doubles["sdk"].news.calls)
    assert first > 0
    capsys.readouterr()

    assert main(run_argv(live_config, "--question-id", str(BINARY))) == EXIT_OK
    assert len(doubles["sdk"].news.calls) == first, "a rerun must not retrieve again"
    assert "(reused, no provider call)" in capsys.readouterr().out

    assert (
        main(run_argv(live_config, "--question-id", str(BINARY), "--refresh-research")) == EXIT_OK
    )
    assert len(doubles["sdk"].news.calls) > first, "--refresh-research must retrieve again"


def test_a_paid_record_whose_evidence_was_not_kept_is_still_written_and_says_so(
    tmp_path: Path, doubles: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The state ``run-replay`` cannot reach, printed by the command that can.

    ``storage.retain_raw_model_output: false`` is an accepted configuration. Under it
    ``run-replay`` **refuses before reading anything**, because the record it would write
    could not be re-derived and it spends nothing, so it can hold that bar (T-903 round-1
    finding 2). This command cannot: the call is paid for, and the cost and invocation count
    are facts whether or not the evidence survived, so the row is appended and the loss is
    reported instead of hidden. That is M1-312's rule, and the ``(none: ...)`` line is how an
    operator learns the record is not replayable.
    """
    data = live_config_data(tmp_path)
    data["storage"]["retain_raw_model_output"] = False
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    open_ledger(load_config(path)).close()

    assert main(run_argv(path, "--question-id", str(BINARY))) == EXIT_OK
    block = printed(capsys.readouterr().out)[0]
    assert block["artifact"] == "(none: retention_disabled)"
    assert block["status"] == "validated"
    assert ledger_rows(path, "SELECT raw_output_path FROM forecast_records") == [(None,)]


def test_the_replay_command_refuses_the_configuration_the_live_one_records_under(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the contrast above, asserted rather than only asserted about.

    Without this, the test above documents an asymmetry that nothing checks, and a future
    change making ``run-replay`` accept the configuration would leave the reasoning in both
    docstrings quietly false.
    """
    from scenario import QUESTION_ID, SEED_ATTEMPT, config_data, seed_scenario, write_config

    data = config_data(tmp_path)
    data["storage"]["retain_raw_model_output"] = False
    seed = seed_scenario(write_config(tmp_path, data))
    assert (
        main(
            [
                "run-replay",
                "--config",
                str(seed.config_file),
                "--question-id",
                str(QUESTION_ID),
                "--snapshot",
                str(SNAPSHOT),
                "--attempt-id",
                SEED_ATTEMPT,
            ]
        )
        == EXIT_REFUSED
    )
    assert "retain_raw_model_output is disabled" in capsys.readouterr().out


def test_the_flags_assert_the_configuration_and_do_not_override_it(
    tmp_path: Path, doubles: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """T-903's rule, carried onto the paid command unchanged."""
    data = live_config_data(tmp_path)
    data["submission"]["dry_run"] = False
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    open_ledger(load_config(path)).close()

    assert main(run_argv(path, "--question-id", str(BINARY), "--dry-run")) == EXIT_REFUSED
    assert "submission.dry_run is not set" in capsys.readouterr().out
    assert ledger_rows(path, "SELECT 1 FROM forecast_records") == []


def test_the_committed_question_ceiling_bounds_the_batch(
    tmp_path: Path, doubles: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """``run_limits.max_questions`` is committed as 1 and had no reader anywhere in ``src/``
    before this item. An operator who has not thought about it gets one question."""
    data = live_config_data(tmp_path)
    del data["run_limits"]["max_questions"]  # fall back to the committed default
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    open_ledger(load_config(path)).close()

    assert main(run_argv(path)) == EXIT_OK
    summary = printed(capsys.readouterr().out)[-1]
    assert summary["records"] == "1 of 1 question(s)"
    assert summary["stopped"] == "question_limit"


# --- the guarantee the criterion names --------------------------------------------------


def imported_by(statements: str) -> set[str]:
    """The ``sys.modules`` delta of some imports, measured in a clean interpreter.

    A subprocess because in-process ``sys.modules`` is polluted by every other test that
    imported an adapter. The shape is ``test_dry_run_acceptance.py``'s.
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


def test_the_replay_pipeline_still_cannot_reach_the_paid_composition() -> None:
    """*"The replay path's structural zero-provider-call guarantee is unchanged."*

    Satisfied by living outside ``pipeline.py`` rather than by restating its test. The three
    guards in ``test_dry_run_acceptance.py`` are the guarantee and are untouched; this asserts
    the specific way this branch could have broken them, which is by making the new module
    reachable from the old one. The import direction may only run the other way.
    """
    added = imported_by("import whiskeyjack_bot.pipeline;")
    assert "whiskeyjack_bot.pipeline_live" not in added
    assert "whiskeyjack_bot.research.orchestrate" not in added
    assert not (added & set(PAID)), sorted(added & set(PAID))


def test_the_live_pipeline_cannot_reach_a_submission_or_approval_module() -> None:
    """A run that spends cannot post. Structural, like T-903's, but a narrower claim.

    ``CODEX_HANDOFF.md`` asks that ``run`` never submit implicitly. A module with no
    submission code on its path is the strongest available form of that, and it is available
    here even though the zero-provider-call form is not.
    """
    added = imported_by("import whiskeyjack_bot.pipeline_live;")
    assert not (added & set(POSTING)), sorted(added & set(POSTING))
    assert not {name for name in added if name.startswith("whiskeyjack_bot.submission")}


def test_the_live_guard_is_not_vacuous() -> None:
    """It must be measuring a graph that really does reach the modules that spend.

    Without this, the test above passes whether or not ``imported_by`` measures anything -- a
    typo'd program string or a module name that never resolves would both read as success.
    ``docs/LESSONS.md`` § 5: check that the check can fail.
    """
    added = imported_by("import whiskeyjack_bot.pipeline_live;")
    assert set(PAID) <= added, sorted(set(PAID) - added)


def test_the_poster_coupling_is_named_rather_than_hidden() -> None:
    """``metaculus.client`` is on the paid graph, and this records exactly why.

    ``research/exa.py`` takes ``MissingCredentialError`` from the module that also holds
    ``build_poster``. That predates this branch, it is filed as its own row, and the honest
    thing is a test that fails when the reason changes -- rather than a forbidden-set that
    quietly omits a module it cannot exclude.
    """
    assert "whiskeyjack_bot.metaculus.client" in imported_by("import whiskeyjack_bot.research.exa;")
    assert "whiskeyjack_bot.metaculus.client" in imported_by(
        "import whiskeyjack_bot.pipeline_live;"
    )


def test_the_replay_command_handler_names_no_paid_module() -> None:
    """The hole T-903's guards leave open, closed by walking the handler itself.

    Those guards are anchored to ``whiskeyjack_bot.pipeline`` and never to
    ``whiskeyjack_bot.cli`` -- and every CLI handler imports its dependencies
    function-locally, so paid code added to the replay handler would trip none of them. An
    ``ast.walk`` over that one function reaches an import statement anywhere inside it.
    """
    from whiskeyjack_bot import cli

    source = inspect.getsource(cli._run_run_replay)
    named: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            named.add(node.module)
    # Anti-vacuity: a walk that found nothing would pass every assertion below.
    assert "whiskeyjack_bot.pipeline" in named
    assert len(named) >= 4

    exercised = imported_by("".join(f"import {name};" for name in sorted(named)))
    assert not (exercised & set(PAID)), sorted(exercised & set(PAID))
    assert not (exercised & set(POSTING)), sorted(exercised & set(POSTING))


def test_the_live_command_handler_is_the_one_that_names_paid_modules() -> None:
    """Anti-vacuity for the handler walk: the same measurement, on the paid handler."""
    from whiskeyjack_bot import cli

    named: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(cli._run_run))):
        if isinstance(node, ast.ImportFrom) and node.module:
            named.add(node.module)
    assert "whiskeyjack_bot.pipeline_live" in named
    exercised = imported_by("".join(f"import {name};" for name in sorted(named)))
    assert set(PAID) <= exercised
