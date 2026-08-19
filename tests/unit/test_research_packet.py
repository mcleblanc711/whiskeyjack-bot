"""M1-306: what the research-packet hash covers, and what it deliberately does not."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    validate_document,
    validate_run,
)
from whiskeyjack_bot.research.packet import (
    PACKET_SCHEMA_VERSION,
    PacketError,
    build_packet,
    canonical_packet_json,
    packet_sha256,
)
from whiskeyjack_bot.research.store import (
    list_retrieval_run_ids,
    load_packet,
    persist_retrieval,
)

WHEN = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
QUESTION = 42


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _run(**overrides: Any) -> ResearchRun:
    payload: dict[str, Any] = {
        "retrieval_run_id": "run-1",
        "question_id": QUESTION,
        "provider": "asknews",
        "provider_config": {"hours_back": 720},
        "queries": ["inflation"],
        "started_at_utc": WHEN,
        "completed_at_utc": WHEN,
    }
    payload.update(overrides)
    return validate_run(payload)


def _document(index: int = 0, **overrides: Any) -> ResearchDocument:
    payload: dict[str, Any] = {
        "retrieval_run_id": "run-1",
        "original_url": f"https://example.org/a{index}",
        "canonical_url": f"https://example.org/a{index}",
        "retrieved_at_utc": WHEN,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": f"{index:064x}",
    }
    payload.update(overrides)
    return validate_document(payload)


# --- what the hash covers ----------------------------------------------------


def test_the_hash_is_computable_from_the_ledger_alone(ledger: sqlite3.Connection) -> None:
    """The property that makes the packet an attribution instrument.

    A forecast stamps the hash it saw (M1-602); if the hash could only be recomputed
    from objects still in memory, that stamp could never be checked against anything.
    """
    run, documents = _run(), [_document(0), _document(1)]
    persist_retrieval(ledger, run, documents)
    stored = load_packet(
        ledger,
        question_id=QUESTION,
        retrieval_run_ids=list_retrieval_run_ids(ledger, question_id=QUESTION),
    )
    assert packet_sha256(stored) == packet_sha256(build_packet(QUESTION, [run], documents))


def test_a_different_provider_config_is_a_different_packet() -> None:
    """The packet records *how* evidence was gathered, not only what was found.

    Two runs that surfaced the same articles under a different provider config are
    not the same research; a hash that could not say so would degrade the packet
    from an attribution record into a reading list.
    """
    documents = [_document(0)]
    base = packet_sha256(build_packet(QUESTION, [_run()], documents))
    changed = packet_sha256(
        build_packet(QUESTION, [_run(provider_config={"hours_back": 24})], documents)
    )
    assert base != changed


def test_a_different_freshness_window_is_a_different_packet() -> None:
    base = packet_sha256(build_packet(QUESTION, [_run()], []))
    changed = packet_sha256(build_packet(QUESTION, [_run(freshness_cutoff_utc=WHEN)], []))
    assert base != changed


def test_an_unknown_cost_and_a_free_run_hash_differently() -> None:
    """``cost_usd is None`` means unknown, never free (M1-303 round 3).

    If the two hashed alike, a run whose spend could not be vouched for would be
    indistinguishable from one that provably cost nothing.
    """
    unknown = packet_sha256(build_packet(QUESTION, [_run(cost_usd=None)], []))
    free = packet_sha256(build_packet(QUESTION, [_run(cost_usd=0.0)], []))
    assert unknown != free


def test_the_schema_version_is_part_of_the_hashed_payload() -> None:
    """So a rule change produces a different digest by construction, not by luck."""
    rendered = json.loads(canonical_packet_json(build_packet(QUESTION, [_run()], [])))
    assert rendered["packet_schema_version"] == PACKET_SCHEMA_VERSION


# --- what the hash excludes --------------------------------------------------


def test_a_reminted_document_id_does_not_move_the_hash(ledger: sqlite3.Connection) -> None:
    """A document's identity here is its dedup key, not the UUID the writer assigns.

    Persisting the same evidence twice mints new UUIDs; if they were hashed, the
    digest would be a fact about when rows were written rather than about evidence.
    """
    run, documents = _run(), [_document(0)]
    persist_retrieval(ledger, run, documents)
    first = load_packet(ledger, question_id=QUESTION, retrieval_run_ids=("run-1",))

    persist_retrieval(
        ledger, _run(retrieval_run_id="run-2"), [_document(0, retrieval_run_id="run-2")]
    )
    stored_ids = {row[0] for row in ledger.execute("SELECT document_id FROM research_documents")}
    assert len(stored_ids) == 2  # two distinct UUIDs for the same article

    # Naming run-1 again returns the same packet it did before run-2 existed, and it
    # still equals the pre-persist hash: neither the freshly minted uuid nor the
    # unrelated second run moves it.
    again = load_packet(ledger, question_id=QUESTION, retrieval_run_ids=("run-1",))
    assert packet_sha256(again) == packet_sha256(first)
    assert packet_sha256(first) == packet_sha256(build_packet(QUESTION, [run], documents))


def test_where_the_raw_bytes_were_filed_does_not_move_the_hash() -> None:
    """These paths are relative to an operator-configured artifact root.

    Hashing them would make the digest machine-dependent: the same evidence would
    fail to verify on a second checkout, and an operator tidying their artifact
    directory could invalidate an attribution record.
    """
    base = packet_sha256(build_packet(QUESTION, [_run()], [_document(0)]))
    relocated = packet_sha256(
        build_packet(
            QUESTION,
            [_run(raw_response_path="somewhere/else.json")],
            [_document(0, raw_artifact_path="a/b.json")],
        )
    )
    assert base == relocated


# --- what a packet refuses to be ---------------------------------------------


def test_a_document_from_a_run_the_packet_does_not_carry_is_refused() -> None:
    """Evidence attributed to a retrieval the packet cannot show."""
    with pytest.raises(PacketError, match="belong to a run the packet carries"):
        build_packet(QUESTION, [_run()], [_document(0, retrieval_run_id="run-9")])


def test_two_documents_sharing_the_ledgers_dedup_key_are_refused() -> None:
    """They are one row, so a packet holding both could not have come from the ledger."""
    with pytest.raises(PacketError, match="deduplicated"):
        build_packet(QUESTION, [_run()], [_document(0), _document(0, title="other")])


def test_a_run_belonging_to_another_question_is_refused() -> None:
    with pytest.raises(PacketError, match="the packet's question"):
        build_packet(QUESTION, [_run(question_id=7)], [])


def test_two_runs_sharing_an_id_are_refused() -> None:
    with pytest.raises(PacketError, match="appears twice"):
        build_packet(QUESTION, [_run(), _run(provider="exa")], [])


def test_a_list_is_refused_where_a_tuple_is_required() -> None:
    """``build_packet`` is the entry point that coerces; the dataclass does not.

    So "it happened to work" and "it is a tuple" cannot come apart.
    """
    from whiskeyjack_bot.research.packet import ResearchPacket

    with pytest.raises(PacketError, match="must be tuples"):
        ResearchPacket(question_id=QUESTION, runs=[_run()], documents=())  # type: ignore[arg-type]
