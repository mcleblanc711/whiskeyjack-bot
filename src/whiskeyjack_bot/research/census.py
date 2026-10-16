"""Language and publisher census of the AskNews evidence already retrieved (M1-356).

Before the local-source lens spends anything, this measures what baseline research already
brings back, which makes it the lens's pre-registration baseline. The 2026-10-16 count over
the live artifacts is the number ``docs/LOCAL-LENS-NOTES.md`` opens with: 52% of AskNews
articles were not in English and none were in Persian. ``_to_document`` discards the
language, so the only place that fact survives is the raw artifact. This reads it there.

**Read-only, and no provider call.** It takes a connection the caller opened read-only
(``ledger.connect_readonly``), runs one ``SELECT``, and reads artifact files through
:func:`artifacts.read_raw_responses`. An artifact that cannot be read is counted, not
raised: a census that stopped at the first lost file would report nothing about the rest.

Every field it reads is **untrusted provider JSON**, so every shape is checked before use. A
value of the wrong type is counted under ``(malformed)``, never coerced. A missing value is
counted under ``(missing)``, never invented -- the rule the sidecar applies too. Language
codes are counted exactly as AskNews wrote them (``en``, ``en-US`` and ``en-us`` are three
rows), because folding them would be a claim about the provider's data this module cannot
check.

Publisher origin comes from :func:`locales.origins` -- the registry's domain lists and
ccTLDs, never the language and never AskNews's ``country``, which the probe showed tags the
story rather than the publisher. AskNews's tag is counted separately, under its own name.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whiskeyjack_bot.research.artifacts import ArtifactError, read_raw_responses
from whiskeyjack_bot.research.locales import LocaleRegistry, origins

MISSING = "(missing)"
MALFORMED = "(malformed)"


class CensusError(Exception):
    """The census could not run at all. Never echoes provider content."""


@dataclass(frozen=True)
class Census:
    """Counts over every article in every readable AskNews artifact.

    ``articles`` counts article rows as retrieved, so an article two runs both returned is
    counted twice. ``unique_urls`` is the number of distinct ``article_url`` strings, which
    is what to quote when asking how much distinct evidence there is.
    """

    runs: int
    unreadable_runs: int
    # Stored bodies or article entries that were not the shape AskNews documents: a body
    # that is not an object, an ``as_dicts`` that is not a list, an article that is not an
    # object. Counted so a census over damaged data says so instead of silently shrinking.
    malformed_entries: int
    articles: int
    unique_urls: int
    languages: dict[str, int]
    provider_story_countries: dict[str, int]
    # "<code>:<origin>" -> articles, e.g. "IR:local". An article local to no locale is
    # counted once under "unclassified".
    publisher_origins: dict[str, int]
    registry_sha256: str


def _text(value: object) -> str:
    if value is None:
        return MISSING
    if type(value) is not str:
        return MALFORMED
    return value if value.strip() else MISSING


def language_census(
    conn: sqlite3.Connection, artifact_root: Path, registry: LocaleRegistry
) -> Census:
    """Count what stored AskNews artifacts say about language, country and publisher."""
    if not isinstance(conn, sqlite3.Connection):
        raise CensusError("conn must be a sqlite3.Connection")
    if not isinstance(registry, LocaleRegistry):
        raise CensusError("registry must be a LocaleRegistry")
    try:
        rows = conn.execute(
            "SELECT retrieval_run_id, raw_response_path FROM research_runs "
            "WHERE provider = 'asknews' AND raw_response_path IS NOT NULL "
            "ORDER BY retrieval_run_id"
        ).fetchall()
    except sqlite3.Error:
        raise CensusError("the ledger could not be read (detail withheld)") from None

    languages: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    publishers: Counter[str] = Counter()
    urls: set[str] = set()
    runs = unreadable = malformed = articles = 0
    for _run_id, relative_path in rows:
        runs += 1
        if type(relative_path) is not str:
            unreadable += 1
            continue
        try:
            bodies = read_raw_responses(artifact_root, relative_path)
        except ArtifactError:
            unreadable += 1
            continue
        for body in bodies:
            found_articles, skipped = _articles(body)
            malformed += skipped
            for article in found_articles:
                articles += 1
                languages[_text(article.get("language"))] += 1
                countries[_text(article.get("country"))] += 1
                url = article.get("article_url")
                if type(url) is str and url:
                    urls.add(url)
                    found = origins(url, registry)
                else:
                    found = {}
                if not found:
                    publishers["unclassified"] += 1
                for code, origin in sorted(found.items()):
                    publishers[f"{code}:{origin}"] += 1
    return Census(
        runs=runs,
        unreadable_runs=unreadable,
        malformed_entries=malformed,
        articles=articles,
        unique_urls=len(urls),
        languages=dict(sorted(languages.items())),
        provider_story_countries=dict(sorted(countries.items())),
        publisher_origins=dict(sorted(publishers.items())),
        registry_sha256=registry.sha256,
    )


def _articles(body: object) -> tuple[list[dict[str, Any]], int]:
    """The article dicts in one stored AskNews response, and how many entries were skipped.

    A body with no ``as_dicts`` key at all is an ordinary empty response, not a malformed
    one; a key present with the wrong type is malformed.
    """
    if not isinstance(body, dict):
        return [], 1
    if "as_dicts" not in body or body["as_dicts"] is None:
        return [], 0
    items = body["as_dicts"]
    if not isinstance(items, list):
        return [], 1
    found = [item for item in items if isinstance(item, dict)]
    return found, len(items) - len(found)
