#!/usr/bin/env python3
"""Confined Codex invocation and fail-closed review acceptance (stdlib only)."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

FAILURE = re.compile(
    r"bwrap:|Failed RTM_NEW|setting up uid map|No permissions to create a new namespace|"
    r"filesystem-restricted execution requires bubblewrap|"
    r"MCP tool call requires approval|error building bubblewrap",
    re.I,
)
REFUSAL = re.compile(
    r"please paste|(?:couldn.t|cannot|can.t|unable to) (?:read|review|access)|"
    r"haven.t reviewed|have not reviewed",
    re.I,
)


def validate(answer, events, diagnostics, head, request):
    """Process success is necessary but never sufficient for a completed review."""
    lines = answer.strip().splitlines()
    if not lines or lines[0] != f"Reviewed commit: {head}":
        raise ValueError("missing or mismatched reviewed commit")
    if not re.search(r"^Verdict: (APPROVE|CHANGES REQUESTED)$", answer, re.M):
        raise ValueError("missing explicit verdict")
    if REFUSAL.search(answer):
        raise ValueError("reviewer reports unavailable context")
    if FAILURE.search(diagnostics):
        raise ValueError("sandbox infrastructure failed")
    complete = False
    inspected = False
    for event in events:
        if event.get("type") in {"error", "turn.failed"}:
            raise ValueError("Codex reported a failed turn")
        complete |= event.get("type") == "turn.completed"
        item = event.get("item", {})
        if event.get("type") != "item.completed":
            continue
        if item.get("type") in {"error", "file_change", "mcp_tool_call", "web_search"}:
            raise ValueError("unexpected tool or error in confined review")
        if item.get("type") == "command_execution":
            if FAILURE.search(item.get("aggregated_output", "")):
                raise ValueError("sandbox tool failed")
            # The prompt requires this exact first inspection. Require actual output,
            # rather than accepting a command that merely mentions the request path.
            output = item.get("aggregated_output", "")
            inspected |= (
                item.get("status") == "completed"
                and item.get("exit_code") == 0
                and request in item.get("command", "")
                and head in output
                and "Request HEAD:" in output
            )
    if not complete or not inspected:
        raise ValueError("missing completed turn or successful brief inspection")


def config_args(root):
    # Do not inherit project hooks, MCP servers, or plugins from another config layer.
    for parent in (root, *root.parents):
        if parent == Path.home():
            break
        if (parent / ".codex/config.toml").exists():
            raise ValueError(
                f"review isolation requires no project config: {parent}/.codex/config.toml"
            )
    codex = Path(shutil.which("codex")).resolve()
    # npm's launcher needs the native helper to be visible during inner re-exec.
    helper = codex.parent.parent if codex.suffix == ".js" else codex.parent
    common = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True
        ).strip()
    )
    gitdir = Path(
        subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], text=True).strip()
    )
    filesystem = {
        ":root": "deny",
        ":minimal": "read",
        str(helper): "read",
        str(common): "read",
        str(gitdir): "read",
    }
    # Enumerate without reading contents. Exact denials avoid bounded glob expansion.
    denied = {".env", ".aws", ".codex", ".ssh", ".kube", ".docker"}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            if name in denied or name.startswith(".env.") or name.endswith(".env"):
                filesystem[str(Path(base) / name)] = "deny"
        dirs[:] = [d for d in dirs if d not in denied | {".git"}]
    filesystem[":workspace_roots"] = {".": "read"}

    # JSON values are valid TOML string literals; tables need TOML '=' separators.
    def toml(value):
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(k) + "=" + toml(v) for k, v in value.items()) + "}"
        return json.dumps(value)

    values = {
        "default_permissions": "reviewer",
        "permissions.reviewer.filesystem": filesystem,
        "permissions.reviewer.network.enabled": False,
        "approval_policy": "never",
        "shell_environment_policy.inherit": "none",
        "shell_environment_policy.set": {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        "features.apps": False,
        "features.code_mode": False,
        "features.code_mode_host": True,
        "web_search": "disabled",
    }
    user_config = Path.home() / ".codex/config.toml"
    if user_config.exists():
        config = tomllib.loads(user_config.read_text())
        for key in ("model", "model_reasoning_effort"):
            if key in config:
                values[key] = config[key]
    args = []
    for key, value in values.items():
        args += ["-c", key + "=" + toml(value)]
    return args


def main():
    request, response, *model = sys.argv[1:]
    root = Path.cwd()
    brief = Path(request).read_text()
    match = re.search(r"\*\*Request HEAD:\*\* `([0-9a-f]{40})`", brief)
    if not match:
        raise ValueError("request has no full pinned HEAD")
    head = match[1]
    if Path(response).exists():
        raise ValueError("refusing to overwrite accepted response")
    args = config_args(root)
    attempt = Path(tempfile.mkdtemp(prefix=f"GPT_REVIEW_ATTEMPT_{Path(response).stem}_", dir=root))
    print(f"Review attempt: {attempt}", flush=True)
    with (attempt / "preflight.log").open("w") as log:
        subprocess.run(
            [
                "codex",
                "sandbox",
                *args,
                "-P",
                "reviewer",
                "--",
                "/bin/sh",
                "-c",
                'git rev-parse HEAD && cat -- "$1"',
                "review-preflight",
                request,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    prompt = (
        f"Perform the cross-model review specified in ./{request}. Follow that complete brief "
        "exactly, including its scope tests, blocking criteria and stopping rule. Do not broaden "
        "the review. First use the shell to run git rev-parse HEAD and cat the complete request. "
        "Inspect relevant repository code with shell commands. No MCP or web tools. "
        f"Your first response line must be exactly: Reviewed commit: {head}\n"
        "Your second line must be Verdict: APPROVE or Verdict: CHANGES REQUESTED. "
        "If you cannot inspect the brief or pinned revision, explain the failure instead of "
        "issuing a verdict. Follow the brief's remaining output requirements."
    )
    candidate = attempt / "candidate.md"
    with (attempt / "events.jsonl").open("w") as events, (attempt / "stderr.log").open("w") as err:
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                *args,
                "-C",
                str(root),
                "--ephemeral",
                "--json",
                *model,
                "-o",
                str(candidate),
                prompt,
            ],
            stdin=subprocess.DEVNULL,
            stdout=events,
            stderr=err,
        )
    if result.returncode:
        raise ValueError(f"Codex exited {result.returncode}; attempt preserved at {attempt}")
    validate(
        candidate.read_text(),
        [json.loads(line) for line in (attempt / "events.jsonl").read_text().splitlines()],
        (attempt / "stderr.log").read_text(),
        head,
        request,
    )
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != head:
        raise ValueError("HEAD changed during review")
    # Atomic no-clobber publication on the same filesystem. Keep the audit candidate.
    os.link(candidate, response)
    print(f"Accepted review: {response}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Review failed; round not accepted: {exc}")
