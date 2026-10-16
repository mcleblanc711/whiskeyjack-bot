"""The read-only language and publisher census over stored AskNews artifacts (M1-356)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.config import AppConfig, load_config
from whiskeyjack_bot.ledger import connect, connect_readonly, initialize_ledger
from whiskeyjack_bot.research.census import (
    MALFORMED,
    MISSING,
    CensusError,
    _articles,
    language_census,
)
from whiskeyjack_bot.research.locales import LocaleRegistry, build_registry
from whiskeyjack_bot.research.model import ResearchRun
from whiskeyjack_bot.research.persist import persist_paid_run

WHEN = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 15, 12, 1, tzinfo=timezone.utc)


def _registry() -> LocaleRegistry:
    return build_registry(
        {
            "schema_version": 1,
            "locales": [
                {
                    "code": "IR",
                    "triggers": ["Iran"],
                    "languages": ["fa"],
                    "cctlds": ["ir"],
                    "local_domains": ["mehrnews.com"],
                    "external_domains": ["bbc.com/persian"],
                }
            ],
        },
        sha256="a" * 64,
    )


def _config(tmp_path: Path) -> AppConfig:
    data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openai/gpt-4o"
    data["retrieval"]["social"]["agent_model"] = "grok-3"
    data["storage"]["artifact_root"] = str(tmp_path / "artifacts")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


def _store(
    conn: sqlite3.Connection, config: AppConfig, run_id: str, provider: str, bodies: list[Any]
) -> None:
    run = ResearchRun.model_validate(
        {
            "retrieval_run_id": run_id,
            "question_id": 7,
            "provider": provider,
            "started_at_utc": WHEN,
            "completed_at_utc": LATER,
        }
    )
    persist_paid_run(conn, config, run, (), raw_responses=tuple(bodies), written_at_utc=LATER)


def _article(url: object, language: object, country: object) -> dict[str, Any]:
    return {"article_url": url, "language": language, "country": country}


@pytest.fixture
def ledger(tmp_path: Path) -> tuple[Path, AppConfig]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    config = _config(tmp_path)
    (tmp_path / "artifacts").mkdir()
    conn = connect(db)
    try:
        _store(
            conn,
            config,
            "run-a",
            "asknews",
            [
                {
                    "as_dicts": [
                        _article("https://www.mehrnews.com/1", "fa", "IR"),
                        _article("https://www.bbc.com/persian/2", "fa", "GB"),
                        _article("http://doc.afp.com/3", "ar", "IR"),
                        _article("https://irna.ir/4", "en-US", "Unknown"),
                    ]
                }
            ],
        )
        _store(
            conn,
            config,
            "run-b",
            "asknews",
            [
                {
                    "as_dicts": [
                        _article("https://www.mehrnews.com/1", None, 5),  # repeat URL
                        _article(None, "", "IR"),
                        "not an article",
                    ]
                },
                {"as_dicts": "not a list"},
                {"no_articles_key": True},
            ],
        )
        _store(
            conn, config, "run-exa", "exa", [{"as_dicts": [_article("https://x.ir/9", "fa", "IR")]}]
        )
    finally:
        conn.close()
    return db, config


def test_the_census_counts_what_asknews_returned_and_classifies_publishers(
    ledger: tuple[Path, AppConfig],
) -> None:
    db, config = ledger
    conn = connect_readonly(db)
    try:
        census = language_census(conn, config.storage.artifact_root, _registry())
    finally:
        conn.close()
    assert (census.runs, census.unreadable_runs) == (2, 0)  # the Exa run is not counted
    # One non-object article and one non-list as_dicts; a body with no as_dicts is empty.
    assert census.malformed_entries == 2
    assert census.articles == 6
    assert census.unique_urls == 4
    assert census.languages == {"ar": 1, "en-US": 1, "fa": 2, MISSING: 2}
    assert census.provider_story_countries == {"GB": 1, "IR": 3, "Unknown": 1, MALFORMED: 1}
    # The AFP copy AskNews tagged IR is unclassified; the BBC Persian copy is external.
    assert census.publisher_origins == {"IR:external": 1, "IR:local": 3, "unclassified": 2}
    assert census.registry_sha256 == "a" * 64


def test_a_body_that_is_not_an_object_is_malformed_not_raised() -> None:
    # The artifact writer refuses such a body, so this shape reaches the census only from a
    # file damaged after writing; the helper is checked directly for that reason.
    assert _articles(["not a body"]) == ([], 1)
    assert _articles(None) == ([], 1)
    assert _articles({"as_dicts": None}) == ([], 0)


def test_a_lost_artifact_is_counted_not_raised(ledger: tuple[Path, AppConfig]) -> None:
    db, config = ledger
    for path in config.storage.artifact_root.rglob("run-a.json"):
        path.unlink()
    conn = connect_readonly(db)
    try:
        census = language_census(conn, config.storage.artifact_root, _registry())
    finally:
        conn.close()
    assert (census.runs, census.unreadable_runs) == (2, 1)


def test_the_census_writes_nothing(ledger: tuple[Path, AppConfig]) -> None:
    db, config = ledger
    before = db.read_bytes()
    conn = connect_readonly(db)
    try:
        language_census(conn, config.storage.artifact_root, _registry())
    finally:
        conn.close()
    assert db.read_bytes() == before


def test_wrong_shapes_raise_the_module_error(ledger: tuple[Path, AppConfig]) -> None:
    db, config = ledger
    with pytest.raises(CensusError):
        language_census("conn", config.storage.artifact_root, _registry())  # type: ignore[arg-type]
    conn = connect_readonly(db)
    try:
        with pytest.raises(CensusError):
            language_census(conn, config.storage.artifact_root, None)  # type: ignore[arg-type]
    finally:
        conn.close()


def test_an_unreadable_ledger_raises_the_module_error(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(CensusError):
        language_census(conn, tmp_path, _registry())
