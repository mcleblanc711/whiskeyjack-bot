"""Acceptance contract for automation: zero process exit is not a review."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "review_runner", Path(__file__).resolve().parents[2] / "scripts/review-runner.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
HEAD = "a" * 40
REQUEST = "GPT_REVIEW_REQUEST_TEST_r1.md"


def events():
    return [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"cat {REQUEST}",
                "aggregated_output": f"- **Request HEAD:** `{HEAD}`",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {"type": "turn.completed"},
    ]


@pytest.mark.parametrize("verdict", ["APPROVE", "CHANGES REQUESTED"])
def test_valid_review(verdict):
    runner.validate(f"Reviewed commit: {HEAD}\nVerdict: {verdict}\n", events(), "", HEAD, REQUEST)


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "Please paste the request here. I haven't reviewed the code.",
        f"Reviewed commit: {'b' * 40}\nVerdict: APPROVE",
        f"Reviewed commit: {HEAD}\nNo blockers",
        f"Reviewed commit: {HEAD}\nVerdict: APPROVE\nI couldn't read the brief.",
    ],
)
def test_invalid_answers(answer):
    with pytest.raises(ValueError):
        runner.validate(answer, events(), "", HEAD, REQUEST)


@pytest.mark.parametrize(
    "evidence,diagnostic",
    [
        ([], ""),
        (events()[:-1], ""),
        ([{"type": "turn.completed"}], ""),
        (events(), "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"),
        (events() + [{"type": "turn.failed"}], ""),
        (events() + [{"type": "item.completed", "item": {"type": "mcp_tool_call"}}], ""),
    ],
)
def test_infrastructure_failures(evidence, diagnostic):
    with pytest.raises(ValueError):
        runner.validate(
            f"Reviewed commit: {HEAD}\nVerdict: APPROVE", evidence, diagnostic, HEAD, REQUEST
        )


def test_failed_inspection_not_accepted():
    evidence = events()
    evidence[0]["item"]["exit_code"] = 1
    with pytest.raises(ValueError):
        runner.validate(f"Reviewed commit: {HEAD}\nVerdict: APPROVE", evidence, "", HEAD, REQUEST)


@pytest.mark.parametrize("valid", [True, False])
def test_publication_and_retry(tmp_path, monkeypatch, valid):
    """A zero-exit non-review is retained, never published or counted as a round."""
    import json
    import subprocess
    import sys

    monkeypatch.chdir(tmp_path)
    Path(REQUEST).write_text(f"- **Request HEAD:** `{HEAD}`")
    response = "GPT_REVIEW_RESPONSE_TEST_r1.md"
    monkeypatch.setattr(sys, "argv", ["review-runner.py", REQUEST, response])
    monkeypatch.setattr(runner, "config_args", lambda root: [])
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: HEAD + "\n")

    def fake_codex(args, **kwargs):
        if "exec" in args:
            candidate = Path(args[args.index("-o") + 1])
            candidate.write_text(
                f"Reviewed commit: {HEAD}\nVerdict: APPROVE" if valid else "Please paste the brief"
            )
            kwargs["stdout"].write("\n".join(json.dumps(e) for e in events()))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_codex)
    if valid:
        runner.main()
        original = Path(response).read_text()
        with pytest.raises(ValueError, match="overwrite"):
            runner.main()
        assert Path(response).read_text() == original
    else:
        for _ in range(2):
            with pytest.raises(ValueError):
                runner.main()
        assert not Path(response).exists()
        assert len(list(tmp_path.glob("GPT_REVIEW_ATTEMPT_*"))) == 2
        assert len(list(tmp_path.glob("GPT_REVIEW_ATTEMPT_*/candidate.md"))) == 2
