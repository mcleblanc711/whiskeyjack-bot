"""M1-357: the document-locale sidecar (019) and the research packet's 1.1.0 rule.

The acceptance criterion in order:

- 019's table refuses an UPDATE, a DELETE, an orphan row and an out-of-vocabulary value.
- Locale rows are written in the same transaction as their documents, and refused before any
  write when they describe something the run does not carry.
- A packet with locale rows hashes under 1.1.0; ``load_packet`` and ``replay_research``
  reproduce it with no provider call.
- ``usable_packet`` and ``without_future`` carry the locale rows of the documents they keep,
  so ``submission_policy.require_research_artifacts`` reproduces a 1.1.0 hash on each of its
  three reconstruction paths.

The 1.0.0 side -- that no packet without locale rows moves -- is
``tests/unit/test_packet_versions.py`` (golden digests from the pre-019 code) and the
oracle property in ``tests/property/test_packet_locale_properties.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
import yaml

from whiskeyjack_bot.config import AppConfig, load_config
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.model import (
    DocumentLocale,
    PublisherOrigin,
    ResearchDocument,
    ResearchSchemaError,
    validate_document,
    validate_locale,
    validate_run,
)
from whiskeyjack_bot.research.packet import (
    PacketError,
    build_packet,
    canonical_packet_json,
    packet_sha256,
)
from whiskeyjack_bot.research.persist import persist_paid_run
from whiskeyjack_bot.research.quality import usable_packet, without_future
from whiskeyjack_bot.research.store import (
    StoreError,
    load_locales,
    load_packet,
    persist_retrieval,
    replay_research,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
QUESTION = 77
RUN = "run-lens-1"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _config(tmp_path: Path, **retrieval: object) -> AppConfig:
    data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openai/gpt-4o"
    data["retrieval"]["social"]["agent_model"] = "grok-3"
    data["storage"]["artifact_root"] = str(tmp_path / "artifacts")
    data["retrieval"].update(retrieval)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


def _run(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "retrieval_run_id": RUN,
        "question_id": QUESTION,
        "provider": "asknews",
        "provider_config": {"lens": "local", "languages": ["ar"], "locale": "OM"},
        "queries": ["Strait of Hormuz closure"],
        "started_at_utc": NOW,
        "completed_at_utc": NOW,
    }
    payload.update(overrides)
    return validate_run(payload)


def _document(name: str, title: str, published: datetime, **overrides: Any) -> ResearchDocument:
    payload: dict[str, Any] = {
        "retrieval_run_id": RUN,
        "original_url": f"https://www.timesofoman.com/{name}",
        "canonical_url": f"https://timesofoman.com/{name}",
        "title": title,
        "snippet": title,
        "retrieved_at_utc": NOW,
        "published_at_utc": published,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": name.encode().hex().ljust(64, "0")[:64],
    }
    payload.update(overrides)
    return validate_document(payload)


def _locale(document: ResearchDocument, **overrides: Any) -> DocumentLocale:
    payload: dict[str, Any] = {
        "retrieval_run_id": document.retrieval_run_id,
        "canonical_url": document.canonical_url,
        "content_sha256": document.content_sha256,
        "locale_code": "OM",
        "publisher_origin": "local",
        "language": "ar",
        "provider_story_country": "OM",
        "native_title": "مضيق هرمز",
        "english_title": document.title,
        "translation_source": "provider:asknews",
        "provider_first_seen_at_utc": NOW - timedelta(hours=1),
        "provider_labels": {"reporting_voice": "Objective", "provocative": "low"},
    }
    payload.update(overrides)
    return validate_locale(payload)


# Three documents chosen so that the policy's three reconstruction paths keep three different
# subsets: `usable` keeps only the relevant, contemporary one; `without_future` also keeps the
# irrelevant one; the raw packet keeps all three.
RELEVANT = _document("a", "Iran threatens to close the Strait of Hormuz", NOW - timedelta(days=1))
IRRELEVANT = _document("b", "Grain prices rise in Brazil", NOW - timedelta(days=1))
FUTURE = _document("c", "Iran closes the Strait of Hormuz", NOW + timedelta(days=1))
DOCUMENTS = (RELEVANT, IRRELEVANT, FUTURE)
LOCALES = tuple(_locale(document) for document in DOCUMENTS)
QUESTION_MODEL = CanonicalBinaryQuestion(
    question_id=QUESTION, post_id=1, title="Will Iran close the Strait of Hormuz?"
)


# --- the model --------------------------------------------------------------------------


def test_the_origin_vocabulary_matches_the_locale_module() -> None:
    from whiskeyjack_bot.research import locales

    assert get_args(PublisherOrigin) == get_args(locales.PublisherOrigin)


def test_an_untranslated_row_carries_no_translation() -> None:
    validate_locale(
        _locale(RELEVANT).model_dump()
        | {"translation_source": "none", "english_title": None, "native_title": "x"}
    )
    # An English original: the English title is the native title.
    validate_locale(
        _locale(RELEVANT).model_dump()
        | {"translation_source": "none", "english_title": "Same", "native_title": "Same"}
    )
    with pytest.raises(ResearchSchemaError) as caught:
        validate_locale(_locale(RELEVANT).model_dump() | {"translation_source": "none"})
    assert "locale_translation_unsourced" in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("locale_code", "Oman"),
        ("publisher_origin", "diaspora"),
        ("translation_source", "provider:exa"),
        ("content_sha256", "XYZ"),
        ("provider_labels", {"x": float("nan")}),
    ],
)
def test_out_of_vocabulary_values_are_refused_without_echo(field: str, value: object) -> None:
    with pytest.raises(ResearchSchemaError) as caught:
        validate_locale(_locale(RELEVANT).model_dump() | {field: value})
    assert "Oman" not in str(caught.value) and "diaspora" not in str(caught.value)


# --- the migration ------------------------------------------------------------------------


def _insert_raw(conn: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "document_id": "missing",
        "locale_code": "OM",
        "publisher_origin": "local",
        "translation_source": "none",
        "created_at_utc": "2026-10-16T12:00:00.000000+00:00",
    }
    row.update(overrides)
    columns = ", ".join(row)
    conn.execute(
        f"INSERT INTO research_document_locales ({columns}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def test_019_refuses_orphans_bad_vocabulary_updates_and_deletes(
    ledger: sqlite3.Connection,
) -> None:
    (document_id,) = persist_retrieval(ledger, _run(), [RELEVANT])
    with pytest.raises(sqlite3.IntegrityError, match="must name an existing document"):
        _insert_raw(ledger)
    for bad in (
        {"publisher_origin": "diaspora"},
        {"translation_source": "provider:exa"},
        {"locale_code": "  "},
        {"translation_source": "none", "english_title": "x", "native_title": "y"},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw(ledger, document_id=document_id, **bad)
    _insert_raw(ledger, document_id=document_id)
    ledger.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("UPDATE research_document_locales SET locale_code = 'IR'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("DELETE FROM research_document_locales")


# --- writing and reading -------------------------------------------------------------------


def test_locale_rows_are_written_with_their_documents_and_read_back(
    ledger: sqlite3.Connection,
) -> None:
    persist_retrieval(ledger, _run(), DOCUMENTS, locales=LOCALES)
    assert load_locales(ledger, RUN) == tuple(sorted(LOCALES, key=DocumentLocale.key))
    stored = ledger.execute(
        "SELECT provider_labels_json FROM research_document_locales LIMIT 1"
    ).fetchone()[0]
    assert json.loads(stored) == {"provocative": "low", "reporting_voice": "Objective"}


def test_a_run_without_locales_writes_no_locale_rows(ledger: sqlite3.Connection) -> None:
    persist_retrieval(ledger, _run(), DOCUMENTS)
    assert ledger.execute("SELECT count(*) FROM research_document_locales").fetchone()[0] == 0
    assert load_locales(ledger, RUN) == ()


@pytest.mark.parametrize(
    ("locales", "message"),
    [
        ((_locale(RELEVANT), _locale(RELEVANT)), "at most one locale"),
        ((_locale(RELEVANT, content_sha256="f" * 64),), "one of this run's documents"),
        ((_locale(RELEVANT, retrieval_run_id="run-other"),), "this run's retrieval_run_id"),
        (("not a locale",), "must be a DocumentLocale"),
    ],
)
def test_a_misattributed_locale_is_refused_before_anything_is_written(
    ledger: sqlite3.Connection, locales: tuple[Any, ...], message: str
) -> None:
    # The message is asserted, not just the type: 019's primary key and the dedup-key check
    # would also refuse the first and third shapes, but inside the transaction and with a
    # message that cannot say which document or why. The pre-checks exist to say so.
    with pytest.raises(StoreError, match=message):
        persist_retrieval(ledger, _run(), DOCUMENTS, locales=locales)
    assert ledger.execute("SELECT count(*) FROM research_runs").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM research_documents").fetchone()[0] == 0


def test_an_opened_run_completes_with_its_locale_rows(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    """The worker's shape: the run row is opened before the billable call, then completed."""
    from whiskeyjack_bot.research.store import open_run

    config = _config(tmp_path)
    config.storage.artifact_root.mkdir(parents=True, exist_ok=True)
    opening = validate_run(
        {
            "retrieval_run_id": RUN,
            "question_id": QUESTION,
            "provider": "asknews",
            "started_at_utc": NOW,
        }
    )
    open_run(ledger, opening)
    persist_paid_run(
        ledger,
        config,
        _run(),
        DOCUMENTS,
        raw_responses=({"as_dicts": []},),
        written_at_utc=NOW,
        run_opened=True,
        locales=LOCALES,
    )
    assert load_locales(ledger, RUN) == tuple(sorted(LOCALES, key=DocumentLocale.key))


def test_a_locale_insert_failure_rolls_back_the_run_and_its_documents(
    ledger: sqlite3.Connection,
) -> None:
    # A trigger refusal inside the transaction, after the documents were inserted: 019's
    # vocabulary check, reached through a row the model cannot produce.
    bad = _locale(RELEVANT).model_copy(update={"publisher_origin": "diaspora"})
    with pytest.raises(StoreError):
        persist_retrieval(ledger, _run(), DOCUMENTS, locales=(bad,))
    assert ledger.execute("SELECT count(*) FROM research_documents").fetchone()[0] == 0


# --- the packet ------------------------------------------------------------------------------


def test_a_packet_with_locale_rows_hashes_under_1_1_0_and_replays(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    persist_retrieval(ledger, _run(), DOCUMENTS, locales=LOCALES)
    loaded = load_packet(ledger, question_id=QUESTION, retrieval_run_ids=[RUN])
    built = build_packet(QUESTION, [_run()], DOCUMENTS, LOCALES)
    assert loaded.locales == tuple(sorted(LOCALES, key=DocumentLocale.key))
    assert packet_sha256(loaded) == packet_sha256(built)
    assert json.loads(canonical_packet_json(loaded))["packet_schema_version"] == "1.1.0"
    bare = build_packet(QUESTION, [_run()], DOCUMENTS)
    assert packet_sha256(loaded) != packet_sha256(bare)

    config = _config(tmp_path, replay_saved_research=True)
    replayed = replay_research(ledger, config, question_id=QUESTION, retrieval_run_ids=[RUN])
    assert packet_sha256(replayed) == packet_sha256(built)


def test_the_constructor_refuses_a_locale_for_no_carried_document() -> None:
    with pytest.raises(PacketError, match="describe a document the packet carries"):
        build_packet(QUESTION, [_run()], [RELEVANT], [_locale(IRRELEVANT)])
    with pytest.raises(PacketError, match="at most one locale"):
        build_packet(QUESTION, [_run()], [RELEVANT], [_locale(RELEVANT), _locale(RELEVANT)])
    with pytest.raises(PacketError):
        build_packet(QUESTION, [_run()], [RELEVANT], ["x"])  # type: ignore[list-item]


def test_the_filters_keep_exactly_the_locale_rows_of_the_documents_they_keep() -> None:
    packet = build_packet(QUESTION, [_run()], DOCUMENTS, LOCALES)
    usable = usable_packet(packet, QUESTION_MODEL, NOW, 30)
    present = without_future(packet, NOW)
    assert {d.title for d in usable.documents} == {RELEVANT.title}
    assert {locale.key() for locale in usable.locales} == {LOCALES[0].key()}
    assert {d.title for d in present.documents} == {RELEVANT.title, IRRELEVANT.title}
    assert {locale.key() for locale in present.locales} == {
        LOCALES[0].key(),
        LOCALES[1].key(),
    }


@pytest.mark.parametrize("path", ["raw", "usable", "without_future"])
def test_the_approval_check_reproduces_a_1_1_0_hash_on_each_reconstruction_path(
    ledger: sqlite3.Connection, tmp_path: Path, path: str
) -> None:
    from whiskeyjack_bot.submission_policy import require_research_artifacts

    config = _config(tmp_path)
    config.storage.artifact_root.mkdir(parents=True, exist_ok=True)
    persist_paid_run(
        ledger,
        config,
        _run(),
        DOCUMENTS,
        raw_responses=({"as_dicts": []},),
        written_at_utc=NOW,
        locales=LOCALES,
    )
    packet = load_packet(ledger, question_id=QUESTION, retrieval_run_ids=[RUN])
    expected = {
        "raw": packet,
        "usable": usable_packet(
            packet, QUESTION_MODEL, NOW, config.retrieval.freshness_days_default
        ),
        "without_future": without_future(packet, NOW),
    }
    digests = {name: packet_sha256(value) for name, value in expected.items()}
    assert len(set(digests.values())) == 3  # three paths, three different packets
    assert all(value.locales for value in expected.values())
    record = SimpleNamespace(
        question=QUESTION_MODEL,
        question_id=QUESTION,
        retrieval_run_id=RUN,
        sources=[],
        research_packet_sha256=digests[path],
        generated_at_utc=NOW,
    )
    require_research_artifacts(ledger, config, record)  # type: ignore[arg-type]


def test_mutating_a_locale_after_construction_cannot_move_the_hash() -> None:
    labels: dict[str, Any] = {"reporting_voice": "Objective"}
    locale = _locale(RELEVANT, provider_labels=labels)
    packet = build_packet(QUESTION, [_run()], [RELEVANT], [locale])
    before = packet_sha256(packet)
    assert locale.provider_labels is not None
    locale.provider_labels["reporting_voice"] = "Persuasive"
    assert packet_sha256(packet) == before
