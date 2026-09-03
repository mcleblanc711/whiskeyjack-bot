"""Exit-code correctness of scripts/gate.sh (T-905).

``run_gate()`` read ``status=$?`` on the line *after* the ``if output="$("$@" 2>&1)"; then
... fi`` block closed. An ``if`` with no ``else`` is itself the compound whose exit status
``$?`` reflects once ``fi`` closes -- 0, unconditionally -- not the status of the command
inside it. So a failing gate printed "FAIL", printed the failing output, printed "the
remaining gates were not run", and then exited 0 anyway. ``set -e`` does not catch this
because a command used as an ``if`` condition is exempt from errexit. The human-readable
output was correct; anything that read the exit code -- a hook, a wrapper, CI -- was not.

The four real gates (ruff, ruff format, mypy, pytest) take ~85s combined and would make this
suite slow for no benefit -- the mechanism under test is entirely in ``run_gate()``'s status
bookkeeping, independent of which command it wraps. So a fake ``uv`` shim on ``PATH``
stands in for the real tools: it always exits 0, unless the test asks it to fail one
specific "gate" (identified by the subcommand gate.sh passes after ``run``), in which case
it prints a marker and exits with a chosen non-zero code. This drives the real
``scripts/gate.sh`` end to end and asserts on ``returncode``, per the acceptance criteria --
grepping stdout for "FAIL" would pass on the pre-fix script too.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SH = REPO_ROOT / "scripts" / "gate.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")

# gate.sh's own gate order; the fake uv shim keys its failure on the tool name gate.sh
# passes as `uv run <tool> ...`.
_TOOL_FOR_LABEL = {
    "ruff check": "ruff",
    "mypy --strict src": "mypy",
}

_FAKE_UV = """\
#!/usr/bin/env bash
# stand-in for `uv`: `uv run <tool> ...` succeeds unless <tool> matches GATE_TEST_FAIL_TOOL,
# in which case it prints a marker to stdout/stderr and exits GATE_TEST_FAIL_CODE.
set -u
shift  # drop "run"
tool="$1"
if [ "${{GATE_TEST_FAIL_TOOL:-}}" = "$tool" ]; then
    echo "fake-$tool-output-marker"
    exit "${{GATE_TEST_FAIL_CODE:-1}}"
fi
exit 0
"""


def _fake_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    uv_path = bin_dir / "uv"
    uv_path.write_text(_FAKE_UV.format(), encoding="utf-8")
    uv_path.chmod(uv_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run_gate_sh(bin_dir: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", **extra_env}
    return subprocess.run(
        ["bash", str(GATE_SH)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_failing_gate_exits_nonzero_with_its_own_status(tmp_path: Path) -> None:
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run_gate_sh(bin_dir, {"GATE_TEST_FAIL_TOOL": "ruff", "GATE_TEST_FAIL_CODE": "3"})

    assert result.returncode == 3, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    assert "fake-ruff-output-marker" in result.stdout
    assert "the remaining gates were not run" in result.stderr
    # ruff check is the first gate; a failure there must stop before the next one runs.
    assert "ruff format --check" not in result.stdout


@pytest.mark.parametrize("label,tool", sorted(_TOOL_FOR_LABEL.items()))
def test_each_gate_can_fail_the_script(tmp_path: Path, label: str, tool: str) -> None:
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run_gate_sh(bin_dir, {"GATE_TEST_FAIL_TOOL": tool, "GATE_TEST_FAIL_CODE": "7"})

    assert result.returncode == 7, result.stdout + result.stderr
    assert f"fake-{tool}-output-marker" in result.stdout


def test_passing_path_exits_zero_with_unchanged_output(tmp_path: Path) -> None:
    bin_dir = _fake_bin_dir(tmp_path)
    result = _run_gate_sh(bin_dir, {})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All four gates pass." in result.stdout
    assert "FAIL" not in result.stdout
