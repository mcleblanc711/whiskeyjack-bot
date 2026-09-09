#!/usr/bin/env python3
"""Tail two or more tournament JSONL logs side by side, tagged and weighted (operator tool).

    scripts/watch-tournaments.py
    scripts/watch-tournaments.py MINIBENCH=data/logs/tournament.jsonl CUP=data/logs/tournament-cup.jsonl

With no arguments, watches the two profiles this repo currently runs. A plain `tail -f` on
these files interleaves them with no indication of which tournament produced which line, and
weighs "posted a prediction" the same as "asked the API for the question list again" -- the
signal an operator actually wants (did it post, did it fail) is buried in routine polling
chatter. This wraps GNU `tail -F` (multiple files, retries across rotation) and reformats each
line: a colored tournament tag, dimmed routine discovery/fetch noise, and prediction/comment
posts highlighted since those are the events that matter most.

Not part of the pipeline the gate runs against -- a terminal convenience script, dependency-free
stdlib only, run directly rather than imported.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_TARGETS = {
    "MINIBENCH": "data/logs/tournament.jsonl",
    "CUP": "data/logs/tournament-cup.jsonl",
}

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
LABEL_COLORS = ["\033[36m", "\033[35m", "\033[33m", "\033[34m", "\033[32m"]  # cyan, magenta, ...
PROMINENT = "\033[1;92m"  # bold bright green
WARN = "\033[33m"
ERROR = "\033[1;31m"

# Order matters: an ERROR/WARNING level line always wins over message-based classification.
_PROMINENT_RE = re.compile(r"post(ed|ing)\s+(prediction|comment)", re.IGNORECASE)
_ROUTINE_RE = re.compile(
    r"^(Retrieving questions from tournament|Returning \d+ questions matching|"
    r"Retrieved \d+ questions from tournament|Retrieving question details for|"
    r"Retrieved question details for)",
)

HEADER_RE = re.compile(r"^==> (.+) <==$")


def _label_for(path_text: str, labels_by_basename: dict[str, str]) -> str:
    return labels_by_basename.get(Path(path_text).name, path_text)


def _format(label: str, color: str, raw: str) -> str:
    raw = raw.rstrip("\n")
    if not raw:
        return ""
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return f"{color}{BOLD}[{label:>9}]{RESET} {raw}"

    ts = str(record.get("ts", ""))[11:19] or "??:??:??"
    level = str(record.get("level", "INFO"))
    message = str(record.get("message", raw))
    tag = f"{color}{BOLD}[{label:>9}]{RESET}"

    if level in ("ERROR", "CRITICAL"):
        return f"{tag} {ts} {ERROR}{BOLD}{level:<8} {message}{RESET}"
    if level == "WARNING":
        return f"{tag} {ts} {WARN}{level:<8} {message}{RESET}"
    if _PROMINENT_RE.search(message):
        return f"{tag} {ts} {PROMINENT}{BOLD}>>> {message}{RESET}"
    if _ROUTINE_RE.match(message):
        return f"{tag} {ts} {DIM}{message}{RESET}"
    return f"{tag} {ts} {message}"


def main(argv: list[str]) -> int:
    targets = dict(DEFAULT_TARGETS)
    if argv:
        targets = {}
        for arg in argv:
            if "=" not in arg:
                print(f"expected LABEL=PATH, got {arg!r}", file=sys.stderr)
                return 2
            label, path = arg.split("=", 1)
            targets[label] = path

    labels_by_basename = {Path(path).name: label for label, path in targets.items()}
    colors_by_label = {
        label: LABEL_COLORS[i % len(LABEL_COLORS)] for i, label in enumerate(targets)
    }

    paths = list(targets.values())
    for path in paths:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).touch(exist_ok=True)

    summary = ", ".join(f"{label}={path}" for label, path in targets.items())
    print(f"watching: {summary}", file=sys.stderr)

    proc = subprocess.Popen(
        ["tail", "-n", "10", "-F", *paths],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    current_label, current_color = next(iter(targets)), colors_by_label[next(iter(targets))]
    try:
        for line in proc.stdout:
            header = HEADER_RE.match(line.rstrip("\n"))
            if header:
                current_label = _label_for(header.group(1), labels_by_basename)
                current_color = colors_by_label.get(current_label, "")
                continue
            formatted = _format(current_label, current_color, line)
            if formatted:
                print(formatted)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
