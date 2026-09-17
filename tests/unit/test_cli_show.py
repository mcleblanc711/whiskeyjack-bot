"""M1-611: the read-only `show` command at the CLI boundary.

What is under test here is the command layer's own behaviour: exit codes, that an unknown
record is refused without echoing it, that the acceptance criterion's core claim holds (a
record with an open submission uncertainty surfaces the exact attempt id `verify-submission
--attempt-id` needs), that the ledger is untouched by an invocation, and that neither the
`show` module nor this command reaches a submission or provider module. The collaborator
functions `show.assemble_show` joins are each already covered by their own module's tests
(`test_approval.py`, `test_lifecycle.py`, `test_submission.py`).
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.config import load_config
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.persist import persist_generation
from whiskeyjack_bot.forecast.record import build_forecast_record_draft
from whiskeyjack_bot.forecast.schema import response_model_for, validate_forecast_response
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    SubmissionAttempt,
    SubmissionVerification,
    record_local_scores,
    record_pre_forecast_failure,
    record_resolution_observation,
    record_submission_attempt,
    record_submission_verification,
    record_validation,
    transaction,
)
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.submission import reserve_submission_key

from resolution_rows import kind_payload

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

QUESTION_ID = 123
TOURNAMENT = "minibench"
RUN_ID = "run-1"
ATTEMPT = "attempt-1"
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
WHEN = datetime(2026, 8, 22, 13, 0, tzinfo=timezone.utc)
TS = "2026-08-22T00:00:00.000000+00:00"

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A valid config whose data paths live under tmp_path (the test_cli_replay shape)."""
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _payload(**overrides: Any) -> dict[str, Any]:
    def block(heading: str) -> str:
        body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
        match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
        assert match is not None, heading
        return match.group(1)

    payload: dict[str, Any] = {
        **json.loads(block("Shared fields")),
        **json.loads("{" + block("Binary schema") + "}"),
    }
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return payload


def _seed(config_file: Path) -> str:
    """Persist one real forecast through the production writer; return its record id."""
    config = load_config(config_file)
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    config.storage.artifact_root.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO research_runs "
                "(retrieval_run_id, provider, started_at_utc, created_at_utc, question_id) "
                "VALUES (?, 'exa', ?, ?, ?)",
                (RUN_ID, TS, TS, QUESTION_ID),
            )
        payload = _payload()
        generation = ForecastGeneration(
            forecast=validate_forecast_response(
                payload, response_model_for(payload["question_type"])
            ),
            settings=ModelSettings(
                provider="openrouter",
                name="openrouter/test-model",
                temperature=0.1,
                max_output_tokens=2048,
                timeout_seconds=60.0,
                allowed_tries=2,
                prompt_version="1.1.0",
                prompt_sha256="b" * 64,
            ),
            sources=tuple(
                SourceReference(
                    source_id=source_id,
                    document_id=None,
                    canonical_url=f"https://example.test/{source_id}",
                    content_sha256="c" * 64,
                )
                for source_id in ("src-001", "src-002")
            ),
            request="the rendered reasoning packet",
            raw_responses=(json.dumps(payload),),
            invocations=1,
            repair_attempted=False,
            cost_usd=0.25,
            failure_code=None,
            failure_problems=(),
        )
        draft = build_forecast_record_draft(
            question=CanonicalBinaryQuestion(
                question_id=QUESTION_ID, post_id=456, title="Will the thing happen?"
            ),
            generation=generation,
            tournament_id=TOURNAMENT,
            attempt_id=ATTEMPT,
            retrieval_run_id=RUN_ID,
            research_packet_sha256="d" * 64,
            generated_at=GENERATED_AT,
        )
        stored = persist_generation(
            conn,
            config,
            draft=draft,
            generation=generation,
            written_at=GENERATED_AT,
        )
        assert stored.record is not None
        assert stored.artifact_outcome == "written"
        return stored.record.record_id
    finally:
        conn.close()


def _approve(config_file: Path, record_id: str) -> None:
    """Validate then approve, through the real writers -- `approve` requires `validated`."""
    config = load_config(config_file)
    conn = connect(config.storage.sqlite_path)
    try:
        record_validation(conn, record_id=record_id, occurred_at=WHEN)
    finally:
        conn.close()
    code = main(
        ["approve", "--config", str(config_file), "--record-id", record_id, "--actor", "chris"]
    )
    assert code == EXIT_OK


POST_ID = 456


def _seed_pre_forecast_failure(config_file: Path) -> None:
    """One failed attempt under `ATTEMPT` before the record that later succeeds under it.

    Must run before :func:`_seed`: `pipeline_failure_events_validate_on_insert` refuses a
    failure for an `attempt_id` that already produced a successful forecast record.
    """
    config = load_config(config_file)
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    try:
        record_pre_forecast_failure(
            conn,
            attempt_id=ATTEMPT,
            question_id=QUESTION_ID,
            tournament_id=TOURNAMENT,
            event_type="research_failed",
            detail_code="provider_unavailable",
            occurred_at=GENERATED_AT - timedelta(minutes=10),
        )
    finally:
        conn.close()


def _drive_full_history(config_file: Path, record_id: str) -> None:
    """Approve, then push the record all the way through submission, resolution and scoring.

    Exercises every category M1-612's merged history covers: an uncertain attempt (so a
    verification is needed to resolve it), a resolution observation, and a local score.
    """
    _approve(config_file, record_id)
    config = load_config(config_file)
    conn = connect(config.storage.sqlite_path)
    try:
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=SubmissionAttempt(
                attempt_id="att-live-1",
                idempotency_key="idem-live-1",
                requested_at_utc=WHEN,
                completed_at_utc=WHEN,
                request_payload_sha256="e" * 64,
                success=True,
                refetch_outcome="absent",
            ),
            occurred_at=WHEN,
            detail_code="refetch_missing",
            secret_env_var_names=(),
        )
        record_submission_verification(
            conn,
            record_id=record_id,
            verification=SubmissionVerification(
                submission_attempt_id="att-live-1",
                outcome="confirmed",
                observed_at_utc=WHEN,
                refetched_forecast_snapshot="{}",
            ),
            occurred_at=WHEN,
        )
        source = kind_payload("binary", "resolved", post_id=POST_ID, question_id=QUESTION_ID)
        record_resolution_observation(
            conn, record_id=record_id, source_response=source, observed_at=WHEN
        )
        record_local_scores(conn, record_id=record_id, computed_at=WHEN)
    finally:
        conn.close()


def _ledger_files(config_file: Path) -> list[Path]:
    config = load_config(config_file)
    base = config.storage.sqlite_path
    return [p for p in (base, base.with_name(base.name + "-wal")) if p.exists()]


def _ledger_hash(config_file: Path) -> str:
    digest = hashlib.sha256()
    for path in _ledger_files(config_file):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_an_unknown_record_is_refused_without_being_echoed(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(config_file)
    bogus = "not-a-real-record-id"
    assert main(["show", "--config", str(config_file), "--record-id", bogus]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused:" in out
    assert bogus not in out


def test_a_fresh_draft_record_shows_empty_sections(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_id = _seed(config_file)
    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"record:    {record_id}" in out
    assert "status:    draft" in out
    assert HEX64.match(out.split("hash:      ", 1)[1].splitlines()[0])
    assert "approval:  none in force" in out
    assert "approval history (0):" in out
    assert "unresolved uncertainties: none" in out
    assert "standing key reservations: none" in out


def test_an_approved_record_shows_the_effective_approval_and_its_payload_binding(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_id = _seed(config_file)
    _approve(config_file, record_id)
    capsys.readouterr()  # discard `approve`'s own printed output
    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    out = capsys.readouterr().out
    assert "status:    approved" in out
    assert "approval:  approved by chris" in out
    approval_line = next(line for line in out.splitlines() if line.strip().startswith("payload:"))
    payload_sha = approval_line.split("payload:", 1)[1].strip()
    assert HEX64.match(payload_sha)
    assert "approval history (1):" in out


def test_a_record_with_an_open_uncertainty_surfaces_the_exact_attempt_id(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The acceptance criterion's core claim: what `verify-submission --attempt-id` needs
    is obtainable from `show` alone, with no artifact file or scrollback required."""
    record_id = _seed(config_file)
    _approve(config_file, record_id)
    capsys.readouterr()  # discard `approve`'s own printed output
    config = load_config(config_file)
    conn = connect(config.storage.sqlite_path)
    try:
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=SubmissionAttempt(
                attempt_id="att-uncertain-1",
                idempotency_key="idem-1",
                requested_at_utc=WHEN,
                completed_at_utc=WHEN,
                request_payload_sha256="e" * 64,
                success=False,
                refetch_outcome="mismatched",
            ),
            occurred_at=WHEN,
            detail_code="refetch_mismatch",
            secret_env_var_names=(),
        )
    finally:
        conn.close()

    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    out = capsys.readouterr().out
    assert "unresolved uncertainties (1):" in out
    assert "attempt att-uncertain-1:" in out
    assert f"verify-submission --record-id {record_id} --attempt-id att-uncertain-1" in out
    lifecycle_line = next(line for line in out.splitlines() if "submission_uncertain" in line)
    assert "detail: refetch_mismatch" in lifecycle_line
    assert "attempt: att-uncertain-1" in lifecycle_line


def test_a_record_with_a_standing_reservation_lists_it(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_id = _seed(config_file)
    _approve(config_file, record_id)
    capsys.readouterr()  # discard `approve`'s own printed output
    config = load_config(config_file)
    conn = connect(config.storage.sqlite_path)
    try:
        reservation = reserve_submission_key(
            conn, record_id=record_id, idempotency_key="idem-standing", reserved_at=WHEN
        )
    finally:
        conn.close()

    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    out = capsys.readouterr().out
    assert "standing key reservations (1):" in out
    assert reservation.reservation_id in out
    assert "idem-standing" in out


def test_every_linked_category_appears_with_an_identifying_field(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M1-612's acceptance criterion, read literally: submission, verification,
    pre-forecast failure, resolution and score events each get their own section."""
    _seed_pre_forecast_failure(config_file)
    record_id = _seed(config_file)
    _drive_full_history(config_file, record_id)
    capsys.readouterr()  # discard `approve`'s own printed output

    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    out = capsys.readouterr().out

    assert "submission attempts (1):" in out
    assert "att-live-1" in out
    assert "submission verifications (1):" in out
    assert "outcome: confirmed" in out
    assert "resolution history (1):" in out
    assert "outcome: yes" in out
    assert "score history (2):" in out
    assert "pre-forecast failures (1):" in out
    assert "research_failed" in out
    assert "status:    scored" in out


def test_the_canonical_history_section_lists_every_category_in_chronological_order(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_pre_forecast_failure(config_file)
    record_id = _seed(config_file)
    _drive_full_history(config_file, record_id)
    capsys.readouterr()

    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    out = capsys.readouterr().out

    block = out.split("canonical history (", 1)[1]
    count = int(block.split("):", 1)[0])
    lines = [line for line in block.splitlines()[1:] if line.startswith("  - ")]
    assert len(lines) == count
    # Every category named in the acceptance criterion appears at least once.
    for kind in (
        "[approval]",
        "[submission_attempt]",
        "[submission_verification]",
        "[lifecycle]",
        "[pre_forecast_failure]",
        "[resolution]",
        "[score]",
    ):
        assert any(kind in line for line in lines), kind
    # The executable form of "chronological order": parse each line's own leading
    # timestamp back out and assert the sequence never goes backwards.
    timestamps = [re.match(r"^\s*-\s*(\S+)\s+\[", line).group(1) for line in lines]  # type: ignore[union-attr]
    assert timestamps == sorted(timestamps)


def test_the_ledger_is_byte_identical_before_and_after_a_full_canonical_history(
    config_file: Path,
) -> None:
    """The byte-identity acceptance criterion, against the fuller ledger M1-612 adds
    reads for -- not a replacement for the simpler existing check above, since a new
    reader touching a table the simpler test never populates could still write."""
    _seed_pre_forecast_failure(config_file)
    record_id = _seed(config_file)
    _drive_full_history(config_file, record_id)
    before = _ledger_hash(config_file)
    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    after = _ledger_hash(config_file)
    assert before == after


def test_the_command_requires_a_record_id(config_file: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["show", "--config", str(config_file)])
    assert caught.value.code == 2


def test_a_mistyped_config_does_not_mint_an_empty_ledger(
    tmp_path: Path, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(config_file)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    elsewhere = tmp_path / "elsewhere" / "bot.sqlite3"
    data["storage"]["sqlite_path"] = str(elsewhere)
    other = tmp_path / "other.yaml"
    other.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert main(["show", "--config", str(other), "--record-id", "rec-1"]) == EXIT_REFUSED
    assert "no ledger database at" in capsys.readouterr().out
    assert not elsewhere.exists()


def test_the_command_makes_no_network_call(config_file: Path) -> None:
    """Belt and braces over the import-graph tests below, which are the real proof.

    ``tests/conftest.py`` blocks sockets and DNS for the whole suite.
    """
    record_id = _seed(config_file)
    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK


def test_the_command_creates_no_log_file(config_file: Path) -> None:
    """`show` deliberately skips `configure_logging` -- round 1's blocking finding.

    Every other handler calls it and that call creates the log directory and opens
    `logging.file` for append: a real write, tolerable for every other command's
    acceptance criterion but not for this one's unqualified "it writes nothing".
    """
    record_id = _seed(config_file)
    config = load_config(config_file)
    assert not config.logging.file.exists()
    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    assert not config.logging.file.exists()
    assert not config.logging.file.parent.exists()


def test_the_ledger_is_byte_identical_before_and_after(config_file: Path) -> None:
    """The executable form of the acceptance criterion's "it writes nothing"."""
    record_id = _seed(config_file)
    _approve(config_file, record_id)
    before = _ledger_hash(config_file)
    assert main(["show", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    after = _ledger_hash(config_file)
    assert before == after


def test_the_show_module_reaches_no_submission_or_provider_module_statically() -> None:
    """The module graph rather than the call path: static import names, not `sys.modules`.

    Mirrors ``test_cli_ingest_resolutions.py::
    test_the_ingest_module_imports_nothing_that_can_post``. ``whiskeyjack_bot.submission``
    itself is expected and safe (pure ledger reads, confirmed by the subprocess test
    below); what must never appear is one of its network-capable siblings.
    """
    tree = ast.parse((REPO_ROOT / "src/whiskeyjack_bot/show.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported, "vacuity guard: the module does import things"
    forbidden = (
        "submission_gateway",
        "submission_live",
        "submission_policy",
        "submission_reconcile",
        "metaculus.client",
        "poster",
    )
    assert not {name for name in imported if any(bad in name for bad in forbidden)}


def test_the_show_module_imports_no_provider_client() -> None:
    """The transitive check the static one can't give: a clean interpreter's
    ``sys.modules`` delta after importing ``whiskeyjack_bot.show`` alone.

    Run out-of-process because in-process ``sys.modules`` is polluted by every other test
    that has already imported an adapter -- checking it here would assert nothing.
    Mirrors ``test_research_store.py::test_the_store_imports_no_provider_client``.
    """
    program = (
        "import sys;"
        "before=set(sys.modules);"
        "import whiskeyjack_bot.show;"
        "print(','.join(sorted(set(sys.modules)-before)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    added = {name for name in result.stdout.strip().split(",") if name}
    forbidden = {
        "asknews_sdk",
        "httpx",
        "forecasting_tools",
        "requests",
        "urllib.request",
        "http.client",
        "ssl",
    }
    assert not (added & forbidden), f"show.py imported: {sorted(added & forbidden)}"
