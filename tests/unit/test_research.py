"""M1-301: the research-run/document schema round-trips, closes its vocabularies,
rejects unusable provenance, hashes content deterministically, withholds inputs
from validation errors, and is storable by the ledger that migration 002 upgrades."""

import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect, initialize_ledger
from whiskeyjack_bot.research import (
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    content_sha256,
    validate_document,
    validate_run,
)

TS = "2026-07-17T00:00:00+00:00"
SHA = "a" * 64


def _document(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "retrieval_run_id": "run-1",
        "original_url": "https://example.org/a?utm_source=x",
        "canonical_url": "https://example.org/a",
        "title": "Payrolls rose in June",
        "publisher": "Example Wire",
        "author": "A. Reporter",
        "published_at_utc": TS,
        "retrieved_at_utc": TS,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": SHA,
        "snippet": "Nonfarm payrolls rose.",
    }
    data.update(overrides)
    return data


def _run(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "retrieval_run_id": "run-1",
        "question_id": 100,
        "provider": "asknews",
        "queries": ["june payrolls"],
        "started_at_utc": TS,
    }
    data.update(overrides)
    return data


def test_document_round_trips() -> None:
    doc = validate_document(_document())
    assert validate_document(doc.model_dump()) == doc


def test_run_round_trips() -> None:
    run = validate_run(_run(completed_at_utc=TS, cost_usd=0.02))
    assert validate_run(run.model_dump()) == run


def test_original_url_is_retained_alongside_canonical_url() -> None:
    # The backlog acceptance criterion: the schema preserves the original URL.
    doc = validate_document(_document())
    assert doc.original_url != doc.canonical_url
    assert doc.original_url.endswith("utm_source=x")


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(reliability="high"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_type", "blog"),
        ("provenance", "scraped"),
        ("reliability_tag", "probably_fine"),
    ],
)
def test_closed_vocabularies_reject_off_list_values(field: str, value: str) -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(**{field: value}))


def test_provider_vocabulary_is_closed() -> None:
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(provider="tavily"))


def test_naive_timestamp_is_rejected() -> None:
    # A naive timestamp is not valid provenance: freshness windows compare these
    # across providers in different zones.
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(retrieved_at_utc="2026-07-17T00:00:00"))


def test_aware_timestamp_is_normalized_to_utc() -> None:
    doc = validate_document(_document(retrieved_at_utc="2026-07-17T02:00:00+02:00"))
    assert doc.retrieved_at_utc == datetime(2026, 7, 17, tzinfo=timezone.utc)
    assert doc.retrieved_at_utc.tzinfo == timezone.utc


def test_malformed_content_hash_is_rejected() -> None:
    for bad in ("not-a-hash", SHA.upper(), "a" * 63):
        with pytest.raises(ResearchSchemaError):
            validate_document(_document(content_sha256=bad))


def test_completion_may_not_precede_start() -> None:
    earlier = (datetime.fromisoformat(TS) - timedelta(minutes=1)).isoformat()
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(completed_at_utc=earlier))


def test_negative_counters_are_rejected() -> None:
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(cost_usd=-0.01))
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(posts_dropped_no_url=-1))


def test_content_hash_is_stable_across_cosmetic_variation() -> None:
    base = content_sha256("Payrolls rose in June.")
    assert content_sha256("  Payrolls\n rose\tin   June.  ") == base
    # NFC: composed vs decomposed accents are the same content.
    assert content_sha256("resumé") == content_sha256("resumé")
    # But real content change, including case, must not collapse.
    assert content_sha256("Payrolls fell in June.") != base
    assert content_sha256("payrolls rose in june.") != base


def test_content_hash_pins_a_known_digest() -> None:
    # Regression guard on the normalization rule itself: changing it breaks
    # replay, so it may only change as a new versioned function.
    assert content_sha256("  hello   world  ") == content_sha256("hello world")
    assert content_sha256("hello world") == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_validation_error_never_echoes_retrieved_content() -> None:
    # A research document carries arbitrary provider text; a credential pasted
    # into a fixture must not surface through a diagnostic or its traceback.
    secret = "sk-live-planted-9d2f1a"
    with pytest.raises(ResearchSchemaError) as excinfo:
        validate_document(_document(source_type=secret))
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert secret not in str(excinfo.value)
    assert secret not in rendered
    assert excinfo.value.__cause__ is None  # a chained ValidationError would re-leak


def test_migration_002_makes_the_document_storable(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 2

    doc = validate_document(_document(document_id="doc-1"))
    run = validate_run(_run())
    conn = connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(research_documents)")}
        assert {"original_url", "provenance"} <= columns
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(research_runs)")}
        assert {"agent_model", "posts_dropped_no_url", "question_id"} <= run_columns

        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, question_id, provider, "
            "started_at_utc, created_at_utc) VALUES (?, ?, ?, ?, ?)",
            (run.retrieval_run_id, run.question_id, run.provider, TS, TS),
        )
        conn.execute(
            "INSERT INTO research_documents (document_id, retrieval_run_id, original_url, "
            "canonical_url, retrieved_at_utc, source_type, provenance, content_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc.document_id,
                doc.retrieval_run_id,
                doc.original_url,
                doc.canonical_url,
                doc.retrieved_at_utc.isoformat(),
                doc.source_type,
                doc.provenance,
                doc.content_sha256,
            ),
        )
        stored = conn.execute(
            "SELECT original_url, provenance FROM research_documents WHERE document_id = 'doc-1'"
        ).fetchone()
        assert stored[0] == doc.original_url
        assert stored[1] == "direct_api"
    finally:
        conn.close()


def test_database_rejects_off_list_provenance(tmp_path: Path) -> None:
    # The CHECK is real, not merely a Pydantic-level convention.
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, started_at_utc, "
            "created_at_utc) VALUES ('run-1', 'asknews', ?, ?)",
            (TS, TS),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO research_documents (document_id, retrieval_run_id, canonical_url, "
                "retrieved_at_utc, content_sha256, provenance) "
                "VALUES ('doc-1', 'run-1', 'https://example.org/a', ?, ?, 'scraped')",
                (TS, SHA),
            )
    finally:
        conn.close()


def test_reapplying_migrations_is_a_no_op(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    conn = connect(db)
    try:
        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert applied == LEDGER_SCHEMA_VERSION
    finally:
        conn.close()


def test_models_are_importable_without_the_questions_package() -> None:
    # M1-301 stays decoupled from M1-201: a run references its question by id.
    assert ResearchRun.model_fields["question_id"].annotation is int
    assert "document_id" in ResearchDocument.model_fields
