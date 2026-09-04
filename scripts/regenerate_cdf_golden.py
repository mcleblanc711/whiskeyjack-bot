#!/usr/bin/env python3
"""Refill the frozen SDK CDF arrays in the numeric-CDF golden fixture (T-904).

    uv run python scripts/regenerate_cdf_golden.py            # rewrite in place
    uv run python scripts/regenerate_cdf_golden.py --check    # exit 1 if it would change

The fixture at ``tests/fixtures/forecasts/numeric_cdf_golden.json`` is a record of what
``forecasting-tools`` actually emits for a set of edge-case inputs, and T-904 exists
because nothing else in the repository holds one: every other CDF assertion is written
against *our* config and *our* guards, so a package upgrade that changed the numbers
without changing their shape would pass all of them.

**The case inputs are hand-authored and this script never invents one.** It reads the
committed ``name``/``why``/``question``/``distribution``/``percentiles`` blocks exactly as
they stand and rewrites only what the SDK decides -- each case's ``cdf`` and
``point_count``, plus ``generated_with.version``. Adding a case is therefore a fixture edit
followed by a run of this script, and the diff a run produces is exactly the drift.

``tests/unit/test_forecast_cdf_golden.py`` deliberately does **not** import this module. It
reads the JSON and calls the SDK itself, so the assertion is a live call against a frozen
record rather than a generator checked against itself.

Run this only for a **deliberate** pin move, and read the array diff before committing it:
a regenerated golden that nobody looked at is the frozen record deleting itself.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any, Final

from forecasting_tools import NumericDistribution, Percentile

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURE: Final = REPO_ROOT / "tests" / "fixtures" / "forecasts" / "numeric_cdf_golden.json"
PACKAGE: Final = "forecasting-tools"


def _emitted(case: dict[str, Any]) -> list[float]:
    """The heights ``get_cdf`` returns for one case, straight from the SDK.

    ``point.percentile``, not ``point.value``: the wire takes the cumulative heights, and
    that is the array ``forecast/cdf.py`` returns and ``submission_live`` validates.

    Nothing here is rounded, sorted or clamped on the way out. ``_standardize_cdf`` already
    ends with ``np.round(cdf, 10)``, which is what makes an exact-equality golden stable;
    doing any of it a second time here would be this script editing the record it exists to
    take.
    """
    question = case["question"]
    distribution = NumericDistribution(
        declared_percentiles=[
            Percentile(percentile=point["percentile"], value=point["value"])
            for point in case["percentiles"]
        ],
        open_upper_bound=question["open_upper_bound"],
        open_lower_bound=question["open_lower_bound"],
        upper_bound=question["upper_bound"],
        lower_bound=question["lower_bound"],
        zero_point=question["zero_point"],
        cdf_size=question["cdf_size"],
        standardize_cdf=case["distribution"]["standardize_cdf"],
        strict_validation=case["distribution"]["strict_validation"],
    )
    return [point.percentile for point in distribution.get_cdf()]


def _rebuilt(golden: dict[str, Any]) -> dict[str, Any]:
    """The fixture with every SDK-decided field refilled and everything else untouched."""
    rebuilt = json.loads(json.dumps(golden))
    rebuilt["generated_with"]["version"] = importlib.metadata.version(PACKAGE)
    for case in rebuilt["cases"]:
        values = _emitted(case)
        case["cdf"] = values
        case["point_count"] = len(values)
    return rebuilt


def _wrapped(values: list[float], *, per_row: int, indent: int) -> str:
    """One frozen array as a JSON list, ``per_row`` values to a line.

    ``json.dumps(..., indent=2)`` puts each of the 201 values on its own line, which makes
    the seven cases a 1,400-line file -- and CLAUDE.md keeps review-request diffs embedded,
    so that file would dominate every request this fixture ever appears in. Wrapping keeps
    the diff readable *and* keeps it pointing at a range of indices, which is the thing a
    drift diff is read for: a whole-array-on-one-line spelling would report "this case
    changed" and nothing more.

    ``repr`` is exactly what ``json.dumps`` uses for a float, and CPython's is the shortest
    string that round-trips, so this reproduces every value bit for bit. :func:`_rendered`
    parses its own output back before returning it, so a value this cannot spell is a hard
    failure rather than a corrupted record.
    """
    pad = " " * indent
    rows = [values[start : start + per_row] for start in range(0, len(values), per_row)]
    body = ",\n".join(pad + ", ".join(repr(value) for value in row) for row in rows)
    return "[\n" + body + "\n" + " " * (indent - 2) + "]"


def _rendered(golden: dict[str, Any]) -> str:
    """One canonical spelling, so a no-op run produces an empty diff."""
    document = json.loads(json.dumps(golden))
    blocks: dict[str, str] = {}
    for index, case in enumerate(document["cases"]):
        cdf_token = f"@@cdf-{index}@@"
        blocks[f'"{cdf_token}"'] = _wrapped(case["cdf"], per_row=5, indent=8)
        case["cdf"] = cdf_token
        # The nine declared points, one pair per line for the same reason: they are the
        # case's input and a reader compares them against the values in the tree they came
        # from, which is four times harder spread over four lines each.
        points_token = f"@@percentiles-{index}@@"
        blocks[f'"{points_token}"'] = (
            "[\n"
            + ",\n".join(
                f'        {{"percentile": {point["percentile"]!r}, "value": {point["value"]!r}}}'
                for point in case["percentiles"]
            )
            + "\n      ]"
        )
        case["percentiles"] = points_token
    text = json.dumps(document, indent=2, ensure_ascii=True)
    for token, block in blocks.items():
        text = text.replace(token, block)
    text += "\n"
    if json.loads(text) != golden:
        raise RuntimeError("the rendered fixture does not read back as what was rendered")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed fixture is not what the SDK emits",
    )
    args = parser.parse_args(argv)

    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rebuilt = _rebuilt(golden)
    rendered = _rendered(rebuilt)

    if args.check:
        if rendered != FIXTURE.read_text(encoding="utf-8"):
            print(f"{FIXTURE} is not what {PACKAGE} emits; run this script without --check")
            return 1
        print(f"{FIXTURE} matches {PACKAGE} {rebuilt['generated_with']['version']}.")
        return 0

    FIXTURE.write_text(rendered, encoding="utf-8")
    print(
        f"Wrote {len(rebuilt['cases'])} case(s) to {FIXTURE} "
        f"from {PACKAGE} {rebuilt['generated_with']['version']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
