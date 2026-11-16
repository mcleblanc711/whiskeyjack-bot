#!/usr/bin/env python3
"""Extract the platform-score fixture for the local continuous log score (M4-809).

    uv run python scripts/regenerate_continuous_score_fixture.py <ledger.sqlite3>
    uv run python scripts/regenerate_continuous_score_fixture.py <ledger.sqlite3> --check

``tests/fixtures/scoring/continuous_platform_scores.json`` is the witness outside this
program for ``scoring.score_continuous``: for numeric and discrete forecasts this account
posted, the CDF the platform holds, the scaling numbers and bound flags Metaculus reported
for the question, the resolution string, and the spot baseline score **Metaculus** computed.
The test reads the JSON and calls the scorer; it does not import this module.

**Read-only, and numbers only.** The ledger is opened with ``mode=ro``. A case holds no
title and no text: a question id, its type, numbers, two flags and the resolution string.

**Selection is deterministic**, in ``question_id`` order: every discrete record (their CDFs
are short), and twelve numeric records -- the two whose resolution sits exactly on a bucket
edge (q45747, q45962), every fourth closed-lower-bound record, and an even spread of the
rest. Re-running against a ledger that has grown adds cases; it never changes one, because a
posted forecast, its question's scaling and its score are fixed once the question resolves.

Run it against a **copy or the live ledger read-only**; never against a ledger a test built.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURE: Final = REPO_ROOT / "tests" / "fixtures" / "scoring" / "continuous_platform_scores.json"

QUERY: Final = """
select f.question_id, f.question_type, r.outcome, r.source_response,
       (select value from score_events s
         where s.forecast_record_id = f.record_id
           and s.metric = 'platform_spot_baseline_score'
         order by event_id desc limit 1) as platform
  from forecast_records f
  join resolution_events r on r.forecast_record_id = f.record_id
 where f.question_type in ('numeric', 'discrete')
   and r.scorable = 1
   and r.event_id in (select max(event_id) from resolution_events group by forecast_record_id)
 order by f.question_id
"""

EDGE_CASES: Final = frozenset({45747, 45962})
NUMERIC_SPREAD: Final = 8


def _question(response: dict[str, Any], question_id: int) -> dict[str, Any]:
    question = response.get("question") or {}
    if question.get("id") == question_id:
        return dict(question)
    members = (response.get("group_of_questions") or {}).get("questions") or []
    return dict(next(member for member in members if member.get("id") == question_id))


def _case(
    question_id: int, kind: str, outcome: str, response: str, platform: float
) -> dict[str, Any]:
    question = _question(json.loads(response), question_id)
    scaling = question["scaling"]
    return {
        "question_id": question_id,
        "question_type": kind,
        "cdf": question["my_forecasts"]["latest"]["forecast_values"],
        "range_min": scaling["range_min"],
        "range_max": scaling["range_max"],
        "zero_point": scaling["zero_point"],
        "open_lower_bound": question["open_lower_bound"],
        "open_upper_bound": question["open_upper_bound"],
        "inbound_outcome_count": scaling["inbound_outcome_count"],
        "resolution": outcome,
        "platform_spot_baseline_score": platform,
    }


def _select(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    discrete = [case for case in cases if case["question_type"] == "discrete"]
    numeric = [case for case in cases if case["question_type"] == "numeric"]
    edges = [case for case in numeric if case["question_id"] in EDGE_CASES]
    ordinary = [case for case in numeric if case["question_id"] not in EDGE_CASES]
    closed = [case for case in ordinary if not case["open_lower_bound"]][::4]
    open_lower = [case for case in ordinary if case["open_lower_bound"]]
    step = max(len(open_lower) // NUMERIC_SPREAD, 1)
    spread = open_lower[::step][:NUMERIC_SPREAD]
    return sorted(discrete + edges + closed + spread, key=lambda case: case["question_id"])


def _render(cases: list[dict[str, Any]], live_count: int) -> str:
    """One case per line, so a diff of the fixture is a diff of cases."""

    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    body = ",\n".join(dump(case) for case in cases)
    lines = [
        "{",
        f'"cases":[\n{body}\n],',
        f'"live_records_with_a_platform_score":{live_count},',
        '"schema_version":"1.0.0",',
        '"source":"whiskeyjack live ledger, read-only; numbers only (M4-809)"',
        "}",
    ]
    return "\n".join(lines) + "\n"


def build(ledger: Path) -> str:
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        rows = [row for row in conn.execute(QUERY) if row[4] is not None]
    finally:
        conn.close()
    cases = [_case(*row) for row in rows]
    return _render(_select(cases), len(cases))


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--check", action="store_true", help="exit 1 if the fixture would change")
    args = parser.parse_args()
    rendered = build(args.ledger)
    if args.check:
        if not FIXTURE.exists() or FIXTURE.read_text(encoding="utf-8") != rendered:
            print("fixture differs from what this ledger gives", file=sys.stderr)
            return 1
        print("fixture is current")
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(rendered, encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
