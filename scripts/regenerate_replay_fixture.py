#!/usr/bin/env python3
"""Derive the stored-packet replay fixtures from the operator's Cup data (M1-330).

    uv run python scripts/regenerate_replay_fixture.py            # rewrite in place
    uv run python scripts/regenerate_replay_fixture.py --check    # exit 1 if it would change

M1-326 stopped a deterministic research verdict being re-purchased, and its tests prove
that over *constructed* articles (the synthetic question 91001). M1-330 is the missing
half: the same gate driven by evidence the system really retrieved. These fixtures are
that evidence, lifted out of Cup question 45452 -- the question whose re-purchase loop
burned 33 billed AskNews calls over 17 retrievals in nine hours and exhausted the
month's quota.

**This script never invents input.** Every article it writes is copied whole and
verbatim from the operator's stored artifact; no field is truncated, rewritten or
synthesized. Truncating one would change its ``content_sha256`` and could change
whether ``research/quality.relevant`` matches it, which would make the fixture a
different packet wearing the same name.

It reads the source tree **read-only** and never writes to it: that tree is live
operational data for a running tournament. The source is not in the repository
(``.gitignore`` excludes ``data/`` at any depth, and CI's tracked-artifact check fails
on it), so this script runs only on the operator's machine and is deliberately not
wired into CI -- neither is ``scripts/regenerate_cdf_golden.py``.

``tests/unit/test_replay_stored_packet.py`` does not import this module. It reads the
JSON and drives it through the real AskNews adapter itself, so the assertion is a live
parse of frozen bytes rather than a generator checked against itself.

Why these four articles, out of the fourteen the run returned. The rule is applied in
order and recorded in each fixture's ``provenance`` block:

1. The one article both AskNews strategies returned, kept in **both** responses. It is
   what preserves the real dedup collapse -- the run stored 14 articles as 13 documents,
   the fixture stores 4 as 3, by the same ``UNIQUE (retrieval_run_id, canonical_url,
   content_sha256)`` key.
2. From the second response, the newest article that is *usable* at the retrieval
   instant. Without one the packet would be refused at that instant too, and the
   fixture could not show that what changed later was freshness.
3. From the second response, the oldest article, which is already stale at the retrieval
   instant. It keeps the packet mixed, so the later refusal cannot be read as "any stale
   document refuses".

Both responses stay non-empty, so one retrieval is still two AskNews calls, never one.

The full run is 82 KB; the largest fixture otherwise committed to this repository is
27 KB. The reduction is the reason this script exists -- so that what was dropped, and
on what rule, is a readable part of the record rather than a judgement made once in a
shell and forgotten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "research"

# The operator's Cup profile, beside this checkout. Overridable because a worktree may
# sit anywhere; rendered in messages because a path is operator-supplied configuration,
# not content (CLAUDE.md's settled M1-401 carve-out).
DEFAULT_SOURCE: Final = REPO_ROOT.parent / "whiskeyjack-bot" / "data" / "cup"

QUESTION_ID: Final = 45452

# The retrieval this fixture replays: the first poll of the loop, 2026-09-08T16:38:33Z.
PACKET_RUN: Final = "run-49cddd480b264fdda316ebf6667796b3"

# Not derived here, deliberately. The 2026-09-09T01:25:19Z poll -- the one `research_failed`
# row this project's live ledgers hold -- was written by `pipeline_live`'s `packet is None`
# branch, because the last run of that retrieval was Exa succeeding with zero documents. It
# never reached `quality_problem`, so replaying it would exercise a different branch and say
# nothing about M1-326's gate. The claim it was wanted for -- that an empty packet reads as
# `no_evidence` and not `stale_evidence` -- is asserted directly on the pure function instead.

POST_FIXTURE: Final = FIXTURES / "cup_45452_post.json"
PACKET_FIXTURE: Final = FIXTURES / "cup_45452_asknews_run.json"


class SourceError(Exception):
    """The operator's Cup tree is not where or what this script expects."""


def _artifact(source: Path, run_id: str) -> dict[str, Any]:
    path = source / "artifacts" / "research" / str(QUESTION_ID) / f"{run_id}.json"
    try:
        envelope: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise SourceError(f"cannot read stored artifact: {path}") from None
    except ValueError:
        raise SourceError(f"stored artifact is not JSON: {path}") from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("raw_responses"), list):
        raise SourceError(f"stored artifact is not a research envelope: {path}")
    return envelope


# The fields the committed ``tests/fixtures/api_posts`` payloads carry, plus ``post_id``
# because this question's post id (45262) and question id (45452) differ -- the synthetic
# fixtures let them coincide, and code that reads one where it means the other would pass
# against those and fail here.
_POST_KEYS: Final = ("id", "published_at", "nr_forecasters", "forecasts_count", "projects")
_QUESTION_KEYS: Final = (
    "id",
    "post_id",
    "type",
    "title",
    "description",
    "resolution_criteria",
    "fine_print",
    "status",
    "open_time",
    "scheduled_close_time",
    "scheduled_resolve_time",
    "cp_reveal_time",
    "actual_resolve_time",
    "resolution",
    "include_bots_in_aggregates",
    "question_weight",
    "label",
    "unit",
    "open_upper_bound",
    "open_lower_bound",
    "scaling",
)


def _reduce_post(post: dict[str, Any]) -> dict[str, Any]:
    """The post, cut down to the fields the committed API-post fixtures already carry.

    Three things go out, and only the first is about size.

    ``aggregations`` carries the **community prediction**, which CLAUDE.md says is never a
    forecaster input in v1. A fixture is not an input, but shipping it invites one.

    ``author_id``/``author_username``/``vote``/``private_note``/``last_viewed_at``/
    ``user_permission`` identify the account that fetched the post -- the operator. None of
    it reaches ``CanonicalQuestion``.

    ``my_forecasts.history`` is the operator's own 200-point forecast on this question, and
    leaving it in would quietly break the test rather than merely bloat it: ``run_once``
    skips a question that already carries a prior at ``if prior.entries``, **before** the
    M1-326 gate and before any retrieval. The replay would then record zero provider calls
    on both polls for a reason that has nothing to do with the gate -- a passing test
    proving nothing, which is the exact defect M1-330 exists to correct. It is set to the
    ``{"history": null}`` the other post fixtures use: null history reads as *empty*, while
    dropping the key entirely reads as *unreadable* and routes to a failure instead.
    """
    question = {k: post["question"][k] for k in _QUESTION_KEYS if k in post["question"]}
    question["my_forecasts"] = {"history": None}
    reduced = {k: post[k] for k in _POST_KEYS if k in post}
    projects = post.get("projects") or {}
    # `category` is kept alongside the two the other post fixtures carry: it is the only
    # dropped field that reaches `CanonicalQuestion`, via `source_categories`, and so the
    # only one whose loss would change `question_fingerprint`. Without it the replayed
    # question is not the question the live worker keyed its verdict on -- a divergence
    # that costs nothing to avoid and would otherwise have to be argued.
    reduced["projects"] = {
        k: projects[k] for k in ("tournament", "default_project", "category") if k in projects
    }
    reduced["question"] = question
    return reduced


def _redact_emails(article: dict[str, Any]) -> dict[str, Any]:
    """``authors[].email`` nulled. The one edit made to an article body, and why.

    The stored bodies carry working email addresses for the journalists who wrote the
    articles -- real personal data about people who are not party to this project. CI
    gitleaks-scans full history on every branch, and a fixture is forever, so it does not
    go in.

    It is provably verdict-neutral: ``asknews._to_document`` reads ``article_url``,
    ``eng_title``/``title``, ``source_id``, ``authors[].name``, ``pub_date``, ``crawl_date``
    and ``summary``, and ``_hash_source`` reads ``full_text``/``summary``/``eng_title``/
    ``title``. No document field, no ``content_sha256`` and no freshness or relevance
    verdict can move. Nulled rather than deleted, because the SDK round-trips the key back
    as ``null`` anyway -- so a nulled body is a fixed point of
    ``SearchResponse.model_validate(...).model_dump(mode="json")`` and the fixture can
    assert that it is one.
    """
    authors = article.get("authors")
    if not isinstance(authors, list):
        return article
    article = dict(article)
    article["authors"] = [
        {**a, "email": None} if isinstance(a, dict) and "email" in a else a for a in authors
    ]
    return article


def _post(source: Path) -> dict[str, Any]:
    """Question 45452's raw Metaculus post, out of whichever poll snapshot carries it."""
    snapshots = sorted((source / "artifacts" / "snapshots").glob("poll-*.json"))
    if not snapshots:
        raise SourceError(f"no poll snapshots under: {source / 'artifacts' / 'snapshots'}")
    for path in snapshots:
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for wrapper in snapshot.get("questions", []):
            data = wrapper.get("data") or {}
            if data.get("id_of_question") == QUESTION_ID:
                post = data.get("api_json")
                if not isinstance(post, dict):
                    raise SourceError(f"snapshot carries no raw post for the question: {path}")
                return _reduce_post(post)
    raise SourceError(f"no snapshot under {snapshots[0].parent} carries the question")


def _pub_date(article: dict[str, Any]) -> str:
    value = article.get("pub_date")
    if not isinstance(value, str):
        raise SourceError("a stored article carries no pub_date; the selection rule needs one")
    return value


def _select(responses: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The four articles, by the rule in the module docstring. Whole, never truncated."""
    if len(responses) != 2:
        raise SourceError("the stored run is not the two-strategy shape the rule assumes")
    first, second = (list(r.get("as_dicts") or []) for r in responses)
    if not first or not second:
        raise SourceError("a stored response carries no articles")

    urls = {a["article_url"] for a in first}
    shared = [a for a in second if a["article_url"] in urls]
    if not shared:
        raise SourceError("no article is common to both strategies; the dedup collapse is lost")
    # Rule 1. Kept on BOTH sides: one copy alone would collapse nothing.
    duplicate = shared[0]
    twin = next(a for a in first if a["article_url"] == duplicate["article_url"])

    others = [a for a in second if a["article_url"] != duplicate["article_url"]]
    if len(others) < 2:
        raise SourceError("the second strategy has too few distinct articles for the rule")
    newest = max(others, key=_pub_date)  # Rule 2.
    oldest = min(others, key=_pub_date)  # Rule 3.
    if newest["article_url"] == oldest["article_url"]:
        raise SourceError("the second strategy's articles share one date; the rule cannot split")
    return [[twin], [oldest, newest, duplicate]]


def _provenance(source_run: str, *, kept: int, available: int, captured: str) -> dict[str, Any]:
    return {
        "question_id": QUESTION_ID,
        "retrieval_run_id": source_run,
        "provider": "asknews",
        "captured_at_utc": captured,
        "articles_kept": kept,
        "articles_in_run": available,
        "edits": [
            "authors[].email set to null -- personal data, provably verdict-neutral",
            "articles reduced by the selection rule below; no article field is truncated",
            "the post fixture drops aggregations, operator-identifying fields, and the "
            "operator's own my_forecasts history",
        ],
        "selection_rule": (
            "Whole articles, copied verbatim, never truncated: (1) the article both "
            "strategies returned, kept on both sides so the run's dedup collapse "
            "survives; (2) the second strategy's newest article, usable at the "
            "retrieval instant; (3) its oldest, already stale at that instant, so the "
            "packet stays mixed. See scripts/regenerate_replay_fixture.py."
        ),
        "regenerate_with": "uv run python scripts/regenerate_replay_fixture.py",
    }


def _rendered(payload: dict[str, Any]) -> str:
    """Indent-2, matching every other fixture in ``tests/fixtures``.

    Keys are **not** re-sorted: ``json.loads`` preserves the source's own order, so a
    stored payload renders in the order the provider wrote it and a reviewer can compare
    it to the artifact line for line. The rendering is still stable, which is what makes
    a no-op run produce an empty diff -- CLAUDE.md keeps review-request diffs embedded,
    so a fixture that reflowed on every run would dominate every request it appears in.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    if json.loads(text) != payload:
        raise SourceError("the rendered fixture does not read back as what was rendered")
    return text


def _build(source: Path) -> dict[Path, str]:
    packet_envelope = _artifact(source, PACKET_RUN)
    responses = packet_envelope["raw_responses"]
    available = sum(len(r.get("as_dicts") or []) for r in responses)
    selected = _select(responses)

    packet = {
        "provenance": _provenance(
            PACKET_RUN,
            kept=sum(len(g) for g in selected),
            available=available,
            captured=packet_envelope["written_at_utc"],
        ),
        # One element per provider call, in call order: the stored response body copied
        # whole, with only `as_dicts` reduced to the selected articles. Every other field
        # is carried verbatim -- `usage`, `hit_cache`, `as_string`, `offset` -- so the body
        # is still exactly a SearchResponse and round-trips to itself through the pinned
        # SDK, which `test_the_committed_bodies_are_real_sdk_responses_that_round_trip`
        # asserts. `usage.credits` differs between the two strategies and is the only
        # record here that they are priced differently; it costs about forty bytes.
        "raw_responses": [
            {**responses[i], "as_dicts": [_redact_emails(a) for a in group]}
            for i, group in enumerate(selected)
        ],
    }

    return {
        POST_FIXTURE: _rendered(_post(source)),
        PACKET_FIXTURE: _rendered(packet),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"the Cup profile directory to read, read-only (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if a committed fixture is not what the source yields",
    )
    args = parser.parse_args(argv)

    try:
        built = _build(args.source)
    except SourceError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check:
        stale = [p for p, text in built.items() if not p.exists() or p.read_text("utf-8") != text]
        if stale:
            for path in stale:
                print(f"{path} is not what {args.source} yields; rerun without --check")
            return 1
        print(f"{len(built)} fixture(s) match {args.source}.")
        return 0

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for path, text in built.items():
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path.relative_to(REPO_ROOT)} ({len(text.encode('utf-8')):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
